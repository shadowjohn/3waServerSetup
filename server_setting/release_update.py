"""Signed releases and code-only transactions; usable by the bootstrap without Textual."""
import argparse
import ast
import base64
from contextlib import contextmanager
from datetime import date
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlparse, quote
import urllib.request
import uuid

REPOSITORY = 'shadowjohn/3waServerSetup'
PRODUCT = '3waServerSetting'
DEFAULT_ROOT = Path('/opt/3waServerSetting')
IDENTITY = '3waServerSetup-release'
NAMESPACE = '3wa-release@3wa.tw'
TAG = re.compile(r'v([0-9]{4})\.([0-9]{2})\.([0-9]{2})\.([0-9]{3})(?:-rc\.([1-9][0-9]*))?')
SHA = re.compile(r'[0-9a-f]{64}')
COMMIT = re.compile(r'[0-9a-f]{40}')
PATH = '/usr/sbin:/usr/bin:/sbin:/bin'
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024
MAX_MANIFEST = 2 * 1024 * 1024
TERMINAL = {'UPDATED', 'UPDATE_ROLLED_BACK', 'UPDATE_FAILED'}
_RUNNING_CODE_ROOT = Path(__file__).resolve().parents[1]


class ReleaseError(RuntimeError):
    def __init__(self, message, code='UPDATE_FAILED'):
        super().__init__(message)
        self.code = code


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _encoded(value):
    return (json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True)+'\n').encode()


def _unique(pairs):
    value = {}
    for name, entry in pairs:
        if name in value:
            raise ReleaseError('Duplicate release metadata field.', 'BAD_MANIFEST')
        value[name] = entry
    return value


def _json(raw):
    if not isinstance(raw,bytes):raise ReleaseError('Required release metadata is missing.','BAD_MANIFEST')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
    except (ValueError, UnicodeError):
        raise ReleaseError('Release metadata is not valid UTF-8 JSON.', 'BAD_MANIFEST') from None


def _inside(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _version(tag):
    match = TAG.fullmatch(tag or '')
    if not match:
        raise ReleaseError('Select a complete vYYYY.MM.DD.NNN or -rc.N tag.', 'INVALID_VERSION')
    year, month, day, sequence, candidate = match.groups()
    try:date(int(year),int(month),int(day))
    except ValueError:raise ReleaseError('Release date is not a real calendar date.','INVALID_VERSION') from None
    if int(sequence)==0:raise ReleaseError('Release sequence starts at 001.','INVALID_VERSION')
    return (int(year),int(month),int(day),int(sequence),1 if candidate is None else 0,int(candidate or 0))


def _payload_path(name):
    if (not isinstance(name,str) or not name or len(name)>240 or '\\' in name or ':' in name or '\x00' in name
            or PurePosixPath(name).is_absolute() or PurePosixPath(name).as_posix()!=name
            or any(part in {'','..','.'} for part in name.split('/'))):
        raise ReleaseError('Unsafe release member path.', 'UNSAFE_ARCHIVE')
    parts = name.split('/')
    fixed = {'run.sh','README.md','LICENSE','nat.sh','firewall_plan.py','requirements-tui.txt','requirements-tui.lock','RELEASE.json'}
    allowed = (name in fixed
        or len(parts)==2 and parts[0]=='server_setting' and re.fullmatch(r'[a-zA-Z0-9_]+\.py',parts[1])
        or len(parts)==2 and parts[0]=='config' and parts[1].endswith(('.example.json','.example.conf'))
        or name=='tests/sqlite-smoke.php' or name=='release/allowed_signers'
        or len(parts)==2 and parts[0]=='wheelhouse' and parts[1].endswith('.whl')
        or parts[0]=='docs' and name.endswith('.md')
        or len(parts)==3 and parts[:2]==['docs','previews'] and re.fullmatch(r'[a-zA-Z0-9_.-]+\.(?:png|svg)',parts[2]))
    if not allowed:
        raise ReleaseError('Release includes a non-distributable member.', 'UNSAFE_ARCHIVE')
    return name


def validate_manifest(raw):
    if not isinstance(raw, bytes) or not 0<len(raw)<=MAX_MANIFEST:
        raise ReleaseError('Release manifest exceeds its bound.', 'BAD_MANIFEST')
    value = _json(raw)
    required = {'schema','product','repository','version','tag','commit','data_schema','python_requires',
                'validated_platforms','archive','installer','files'}
    if not isinstance(value,dict) or not required<=set(value) or set(value)-required-{'created_utc','created_at'}:
        raise ReleaseError('Unknown release manifest schema.', 'BAD_MANIFEST')
    if (type(value['schema']) is not int or value['schema']!=1 or value['product']!=PRODUCT or value['repository']!=REPOSITORY
            or not isinstance(value['tag'],str) or value['version']!=value['tag'][1:]
            or not isinstance(value['commit'],str) or not COMMIT.fullmatch(value['commit'])):
        raise ReleaseError('Release identity is invalid.', 'BAD_MANIFEST')
    _version(value['tag'])
    if (type(value['data_schema']) is not int or value['data_schema']!=1
            or value['python_requires'] not in ('>=3.12','>=3.10')):
        raise ReleaseError('Release data or Python compatibility is unsupported.', 'INCOMPATIBLE_RELEASE')
    platforms=value['validated_platforms']
    if (not isinstance(platforms,list)
            or any(not isinstance(p,str) or p not in {'ubuntu22.04','ubuntu24.04','ubuntu26.04'} for p in platforms)
            or len(platforms)!=len(set(platforms))):
        raise ReleaseError('Unknown validated platform declaration.', 'BAD_MANIFEST')
    for kind,expected in [('archive',PRODUCT+'-'+value['tag']+'.tar.gz'),('installer','install.sh')]:
        asset=value[kind]
        if (not isinstance(asset,dict) or set(asset)!={'name','size','sha256'} or asset['name']!=expected
                or type(asset['size']) is not int or not 0<asset['size']<=MAX_ARCHIVE
                or not isinstance(asset['sha256'],str) or not SHA.fullmatch(asset['sha256'])):
            raise ReleaseError('Release asset identity is invalid.', 'BAD_MANIFEST')
    files=value['files']
    if not isinstance(files,dict) or not 1<=len(files)<=2048 or len({p.casefold() for p in files})!=len(files):
        raise ReleaseError('Release file set is invalid.', 'BAD_MANIFEST')
    expanded=0
    for name, entry in files.items():
        _payload_path(name)
        if (not isinstance(entry,dict) or set(entry)!={'sha256','size','mode'}
                or type(entry['size']) is not int or not 0<=entry['size']<=MAX_EXPANDED
                or type(entry['mode']) is not int or entry['mode'] not in {0o644,0o755}
                or not isinstance(entry['sha256'],str) or not SHA.fullmatch(entry['sha256'])):
            raise ReleaseError('Release file metadata is invalid.', 'BAD_MANIFEST')
        expanded+=entry['size']
    must={'run.sh','server_setting/__init__.py','server_setting/__main__.py','server_setting/root_actions.py',
          'server_setting/release_update.py','requirements-tui.lock','config/firewall.example.json','release/allowed_signers','RELEASE.json'}
    if not must<=set(files) or not any(p.startswith('wheelhouse/') for p in files) or expanded>MAX_EXPANDED:
        raise ReleaseError('Release runtime payload is incomplete or too large.', 'BAD_MANIFEST')
    return value


def ensure_current_release(code_root, allow_pending=False):
    """Call with a physical path captured at import, never a newly resolved alias."""
    code_root=Path(code_root)
    if code_root.parent.name!='releases':
        return
    root=code_root.parent.parent
    current=root/'current'
    if not current.is_symlink() or current.resolve()!=code_root:
        raise ReleaseError('The installed program changed; reopen the console before modifying the host.', 'UPDATE_RESTART_REQUIRED')
    journal=root/'release/journal.json'
    if not allow_pending and journal.is_file():
        value=_json(journal.read_bytes())
        if value.get('status') not in TERMINAL:
            raise ReleaseError('An interrupted program update needs recovery first.', 'RECOVERY_REQUIRED')


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        _url(new_url, redirect=True)
        return super().redirect_request(request,file,code,message,headers,new_url)


def _url(value, redirect=False):
    parsed=urlparse(value)
    hosts={'api.github.com','github.com'}
    if redirect:
        hosts|={'release-assets.githubusercontent.com','objects.githubusercontent.com'}
    if parsed.scheme!='https' or parsed.hostname not in hosts or parsed.username or parsed.password or parsed.port not in {None,443}:
        raise ReleaseError('An unexpected release download location was refused.', 'UNSAFE_SOURCE')
    if not redirect and not (parsed.path.startswith('/repos/'+REPOSITORY+'/') if parsed.hostname=='api.github.com'
                             else parsed.path.startswith('/'+REPOSITORY+'/releases/download/')):
        raise ReleaseError('Release source must be the fixed project repository.', 'UNSAFE_SOURCE')
    return value


class ReleaseManager:
    def __init__(self, root=None, fetcher=None, runner=None):
        requested=Path(root or DEFAULT_ROOT)
        if '..' in requested.parts or requested.parent==requested:
            raise ReleaseError('Installation root must be a dedicated path without parent traversal.','INVALID_ROOT')
        self.root=requested.absolute()
        if os.name=='posix' and self.root.anchor!='/':
            raise ReleaseError('Installation root must use a canonical absolute POSIX path.','INVALID_ROOT')
        if os.name=='posix' and self.root.exists() and DEFAULT_ROOT.exists():
            if any(part.is_symlink() for part in (self.root,*self.root.parents)):
                raise ReleaseError('Installation root may not contain symbolic links.','UNSAFE_PATH')
            if self.root.samefile(DEFAULT_ROOT):self.root=DEFAULT_ROOT
        self.fetcher=fetcher
        self.runner=runner
        self.control=self.root/'release'
        self.releases=self.root/'releases'
        self.state_file=self.control/'state.json'
        self.journal_file=self.control/'journal.json'
        self.production=self.root==DEFAULT_ROOT
        self.runtime=Path('/run/3wa-server-setup') if self.production else self.root/'.update'
        self.transactions=Path('/var/lib/3wa-server-setup/transactions') if self.production else self.runtime/'transactions'

    def _gate(self):
        if platform.system()!='Linux' or getattr(os,'geteuid',lambda:-1)()!=0:
            raise ReleaseError('Installing or changing the program requires Linux root.', 'ROOT_REQUIRED')
        try:
            fields=dict(line.split('=',1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
        except OSError:
            fields={}
        if fields.get('ID','').strip('"')!='ubuntu' or fields.get('VERSION_ID','').strip('"') not in {'24.04','26.04'}:
            raise ReleaseError('Release installation supports Ubuntu 24.04 and 26.04 only.', 'UNSUPPORTED_OS')
        if sys.version_info<(3,12):
            raise ReleaseError('Release installation requires Python 3.12 or newer.', 'UNSUPPORTED_PYTHON')

    @staticmethod
    def _active_code(allow_pending=False):
        ensure_current_release(_RUNNING_CODE_ROOT,allow_pending=allow_pending)

    def _trusted(self, path, directory=False):
        path=Path(path)
        if path.is_symlink():
            raise ReleaseError('An unexpected symbolic link was refused.', 'UNSAFE_PATH')
        info=path.lstat()
        if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
            raise ReleaseError('Unexpected release filesystem object.', 'UNSAFE_PATH')
        if os.name=='posix' and (info.st_uid!=0 or info.st_mode&0o022):
            raise ReleaseError('Release data must be root controlled.', 'UNSAFE_PATH')
        for parent in path.parents:
            if parent.is_symlink():raise ReleaseError('Release parent is a symbolic link.', 'UNSAFE_PATH')
            if os.name=='posix':
                metadata=parent.stat()
                sticky=bool(metadata.st_mode&stat.S_ISVTX)
                if metadata.st_uid!=0 or metadata.st_mode&0o022 and not sticky:
                    raise ReleaseError('A release parent is not root controlled.', 'UNSAFE_PATH')

    def _dir(self,path,private=False):
        path=Path(path)
        missing=[]; current=path
        while not current.exists():
            if current.is_symlink():raise ReleaseError('Broken release directory alias.', 'UNSAFE_PATH')
            missing.append(current);current=current.parent
        self._trusted(current,True)
        for child in reversed(missing):
            mode=0o700 if private and child==path else 0o755
            child.mkdir(mode=mode);child.chmod(mode)
        self._trusted(path,True)

    def _read(self,path,limit=MAX_MANIFEST,missing=None):
        path=Path(path)
        if not path.exists() and not path.is_symlink():return missing
        self._trusted(path)
        with path.open('rb') as stream:raw=stream.read(limit+1)
        if len(raw)>limit:raise ReleaseError('Release state exceeds its bound.', 'UNSAFE_STATE')
        return raw

    def _atomic(self,path,raw,mode=0o644):
        if not isinstance(raw,bytes):raise ReleaseError('Required release content is missing.','INCOMPLETE_RELEASE')
        path=Path(path);self._dir(path.parent)
        if path.exists() or path.is_symlink():self._trusted(path)
        fd,temporary=tempfile.mkstemp(prefix='.3wa-release-',dir=path.parent)
        try:
            with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
            os.chmod(temporary,mode);os.replace(temporary,path);self._sync(path.parent)
        finally:
            if os.path.exists(temporary):os.unlink(temporary)

    @staticmethod
    def _sync(path):
        if os.name=='posix':
            descriptor=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(descriptor)
            finally:os.close(descriptor)

    def _command(self,argv,timeout=30,input=None,cwd=None):
        if self.runner is not None:
            return self.runner(argv,timeout=timeout,input=input,cwd=cwd)
        executable=argv[0]
        if not os.path.isabs(executable):
            executable=shutil.which(executable,path=PATH) if os.name=='posix' else shutil.which(executable)
            if not executable:raise ReleaseError('A required release command is unavailable.', 'MISSING_COMMAND')
        resolved=Path(executable).resolve(strict=True)
        if os.name=='posix':
            for part in (resolved,*resolved.parents):
                info=part.stat()
                if info.st_uid!=0 or info.st_mode&0o022:raise ReleaseError('A release command is not root controlled.', 'UNSAFE_COMMAND')
        env={'PATH':PATH,'HOME':'/root','LANG':'C','LC_ALL':'C','PYTHONNOUSERSITE':'1','GIT_CONFIG_NOSYSTEM':'1',
             'GIT_CONFIG_GLOBAL':'/dev/null','GIT_OPTIONAL_LOCKS':'0'} if os.name=='posix' else dict(os.environ)
        try:
            return subprocess.run([str(executable),*argv[1:]],input=input,capture_output=True,timeout=timeout,cwd=cwd,env=env,
                                  **({'umask':0o022} if os.name=='posix' else {}))
        except (OSError,subprocess.TimeoutExpired):
            raise ReleaseError('A bounded release command failed or timed out.', 'COMMAND_FAILED') from None

    def _run(self,argv,timeout=30,input=None,cwd=None):
        return self._command(argv,timeout,input,cwd)

    def _checked(self,argv,timeout=30,input=None,cwd=None,code='COMMAND_FAILED'):
        result=self._run(argv,timeout=timeout,input=input,cwd=cwd)
        if result.returncode:raise ReleaseError('A release preparation check failed.',code)
        return result

    def _fetch(self,url,limit):
        _url(url)
        try:
            if self.fetcher is not None:raw=self.fetcher(url,limit)
            else:
                request=urllib.request.Request(url,headers={'Accept':'application/vnd.github+json','User-Agent':'3waServerSetting-release/1'})
                with urllib.request.build_opener(_Redirect()).open(request,timeout=20) as response:raw=response.read(limit+1)
        except ReleaseError:raise
        except Exception:raise ReleaseError('The fixed release source could not be downloaded.', 'DOWNLOAD_FAILED') from None
        if not isinstance(raw,bytes) or not 0<len(raw)<=limit:raise ReleaseError('Release response exceeds its bound.', 'DOWNLOAD_FAILED')
        return raw

    def _signers(self, supplied=None):
        installed=self._read(self.control/'allowed_signers',65536)
        if supplied is not None:
            raw=self._read(Path(supplied),65536)
            if installed is not None and raw!=installed:raise ReleaseError('The installed release signing identity cannot be changed by an update.', 'SIGNER_CHANGED')
        else:
            raw=installed
            if raw is None:raw=self._read(Path(__file__).resolve().parents[1]/'release/allowed_signers',65536)
        if not raw or not raw.startswith((IDENTITY+' ').encode()) or b'PRIVATE KEY' in raw:
            raise ReleaseError('The pinned public release signer is unavailable.', 'SIGNER_REQUIRED')
        return raw

    def _verify_signature(self,raw,signature,signers):
        if not isinstance(raw,bytes) or not 0<len(raw)<=MAX_MANIFEST:
            raise ReleaseError('Release manifest is missing or exceeds its bound.','BAD_MANIFEST')
        if not isinstance(signature,bytes) or not 0<len(signature)<=65536:
            raise ReleaseError('Release signature is missing.', 'BAD_SIGNATURE')
        with tempfile.TemporaryDirectory(prefix='threewa-verify-') as directory:
            keyfile=Path(directory)/'allowed_signers';keyfile.write_bytes(signers)
            sigfile=Path(directory)/'manifest.sig';sigfile.write_bytes(signature)
            self._checked(['ssh-keygen','-Y','verify','-f',str(keyfile),'-I',IDENTITY,'-n',NAMESPACE,'-s',str(sigfile)],
                          input=raw,timeout=15,code='BAD_SIGNATURE')

    @staticmethod
    def _identity(manifest,directory=None):
        result={key:manifest[key] for key in ('version','tag','commit','data_schema','validated_platforms')}
        if directory is not None:result['directory']=directory
        if manifest.get('legacy'):result['legacy']=True
        return result

    def _current(self):
        path=self.root/'current'
        if not path.exists() and not path.is_symlink():return None
        if not path.is_symlink():raise ReleaseError('Current program pointer is not a managed link.', 'UNSAFE_PATH')
        target=os.readlink(path)
        if not re.fullmatch(r'releases/[a-zA-Z0-9_.-]+',target):raise ReleaseError('Current program pointer is outside the release directory.', 'UNSAFE_PATH')
        directory=self.root/target
        self._trusted(directory,True)
        return directory.name

    def _state(self):
        raw=self._read(self.state_file)
        if raw is None:return {'schema':1,'current':None,'previous':None}
        value=_json(raw)
        if not isinstance(value,dict) or value.get('schema')!=1 or set(value)-{'schema','current','previous','launcher_sha256'} or not {'schema','current','previous'}<=set(value):
            raise ReleaseError('Installed release state is invalid.', 'UNSAFE_STATE')
        for name in ('current','previous'):
            if value[name] is not None and not re.fullmatch(r'[a-zA-Z0-9_.-]+',value[name]):raise ReleaseError('Installed release state is invalid.', 'UNSAFE_STATE')
        return value

    def _journal(self):
        raw=self._read(self.journal_file)
        if raw is None:return None
        value=_json(raw)
        if not isinstance(value,dict) or value.get('schema')!=1 or not isinstance(value.get('transaction_id'),str):
            raise ReleaseError('Release recovery journal is invalid.', 'UNSAFE_STATE')
        return value

    def _installed_manifest(self,name,verify=False):
        if not re.fullmatch(r'[a-zA-Z0-9_.-]+',name or ''):raise ReleaseError('Invalid installed version directory.', 'UNSAFE_STATE')
        directory=self.releases/name;self._trusted(directory,True)
        manifest=_json(self._read(directory/'.release-manifest.json'))
        if manifest.get('legacy'):
            if manifest.get('data_schema')!=1 or manifest.get('version')!='legacy':raise ReleaseError('Legacy snapshot state is invalid.', 'UNSAFE_STATE')
        else:validate_manifest(_encoded(manifest))
        if verify:
            ready=_json(self._read(directory/'.release-ready.json'))
            if ready!={'schema':1,'manifest_sha256':_digest(_encoded(manifest)),'smoke_verified':True}:
                raise ReleaseError('Installed release preparation did not complete.','INCOMPLETE_RELEASE')
            if not manifest.get('legacy'):
                original=self._read(directory/'.signed-manifest.json')
                signature=self._read(directory/'.signed-manifest.sig',65536)
                self._verify_signature(original,signature,self._signers())
                if validate_manifest(original)!=manifest:
                    raise ReleaseError('Stored release provenance was modified.','EXTERNAL_EDIT')
            for relative,entry in manifest['files'].items():
                _payload_path(relative)
                raw=self._read(directory/relative,MAX_EXPANDED)
                if raw is None or len(raw)!=entry['size'] or _digest(raw)!=entry['sha256']:
                    raise ReleaseError('An installed program file changed outside the updater.', 'EXTERNAL_EDIT')
                if os.name=='posix' and stat.S_IMODE((directory/relative).stat().st_mode)!=entry['mode']:
                    raise ReleaseError('Installed program permissions changed outside the updater.','EXTERNAL_EDIT')
            self._trusted(directory/'.venv',True)
            interpreter=directory/'.venv/bin/python'
            if not interpreter.exists():raise ReleaseError('Installed release interpreter is missing.', 'INCOMPLETE_RELEASE')
            self._trusted(interpreter.resolve())
        return manifest

    def status(self):
        try:
            name=self._current();state=self._state()
            try:journal=self._journal()
            except PermissionError:
                raw=self._read(self.control/'pending.json')
                public=_json(raw) if raw else {'pending':None}
                journal=public.get('pending')
            pending=journal if journal and journal.get('status') not in TERMINAL else None
            if name!=state['current'] and not pending:
                raise ReleaseError('The installed pointer and recorded version disagree.','EXTERNAL_EDIT')
            installed=self._identity(self._installed_manifest(name),name) if name else None
            previous=state.get('previous')
            older=self._identity(self._installed_manifest(previous),previous) if previous else None
            return {'supported':True,'managed':bool(name),'installed':installed,'previous':older,
                'pending':{'transaction_id':pending['transaction_id'],'status':pending['status'],'recoverable':True} if pending else None,
                'detail':'Explicit signed program updates only; host settings and packages are preserved.' if name else 'Use the release installer to initialize or adopt this flat installation.'}
        except (ReleaseError,OSError,TypeError,KeyError):
            return {'supported':False,'managed':False,'installed':None,'previous':None,'pending':None,
                    'detail':'Installed release state needs root inspection; no update was performed.'}

    def _remote(self,tag=None):
        if tag is not None:_version(tag)
        endpoint='https://api.github.com/repos/'+REPOSITORY+'/releases/'+('tags/'+quote(tag,safe='') if tag else 'latest')
        api=_json(self._fetch(endpoint,MAX_MANIFEST))
        if not isinstance(api,dict):raise ReleaseError('Invalid release API response.','BAD_RELEASE')
        selected=api.get('tag_name');_version(selected)
        if (tag and selected!=tag) or api.get('draft') or (tag is None and api.get('prerelease')):
            raise ReleaseError('Release selection does not match the requested channel.', 'BAD_RELEASE')
        base='https://github.com/'+REPOSITORY+'/releases/download/'+selected+'/'
        assets=api.get('assets')
        if not isinstance(assets,list):raise ReleaseError('Release asset listing is missing.', 'BAD_RELEASE')
        by_name={}
        for asset in assets:
            name=asset.get('name')
            if name in by_name:raise ReleaseError('Release has duplicate assets.', 'BAD_RELEASE')
            by_name[name]=asset.get('browser_download_url')
        for name in ('release-manifest.json','release-manifest.json.sig'):
            if by_name.get(name)!=base+name:raise ReleaseError('Release manifest asset is not at the fixed project source.', 'BAD_RELEASE')
        raw=self._fetch(base+'release-manifest.json',MAX_MANIFEST)
        signature=self._fetch(base+'release-manifest.json.sig',65536)
        self._verify_signature(raw,signature,self._signers())
        manifest=validate_manifest(raw)
        if manifest['tag']!=selected:raise ReleaseError('Signed release tag disagrees with the selected release.', 'BAD_RELEASE')
        archive=manifest['archive']
        if by_name.get(archive['name'])!=base+archive['name']:raise ReleaseError('Release archive asset is missing.', 'BAD_RELEASE')
        commit=_json(self._fetch('https://api.github.com/repos/'+REPOSITORY+'/commits/'+selected,MAX_MANIFEST))
        if not isinstance(commit,dict) or commit.get('sha')!=manifest['commit']:raise ReleaseError('Release tag moved away from its signed commit.', 'TAG_MOVED')
        return {'manifest':manifest,'raw':raw,'signature':signature,'archive_url':base+archive['name'],
                'notes':str(api.get('body') or '')[:65536],'published_at':str(api.get('published_at') or '')[:80]}

    def check(self,tag=None):
        state=self.status()
        if not state['supported']:raise ReleaseError(state['detail'],'UNSAFE_STATE')
        remote=self._remote(tag);manifest=remote['manifest'];installed=state['installed']
        if installed and installed.get('version')==manifest['version'] and installed['commit']!=manifest['commit']:
            raise ReleaseError('An installed version was republished with a different commit.', 'VERSION_CONFLICT')
        same=bool(installed and installed['version']==manifest['version'] and installed['commit']==manifest['commit'])
        release=dict(self._identity(manifest),archive=manifest['archive'],signature_verified=True)
        result={'status':'UP_TO_DATE' if same else 'UPDATE_AVAILABLE','current':installed,'release':release,
                'notes':remote['notes'],'published_at':remote['published_at'],'manifest_sha256':_digest(remote['raw'])}
        result['fingerprint']=_digest(_encoded(result))
        return result

    def plan(self,tag=None):
        state=self.status()
        if not state['supported']:raise ReleaseError(state['detail'],'UNSAFE_STATE')
        if not state['managed']:raise ReleaseError('Run the release installer before using in-place updates.','UNMANAGED_INSTALL')
        if state['pending']:raise ReleaseError('Recover the interrupted program update first.','RECOVERY_REQUIRED')
        value=self.check(tag)
        if not state['installed'].get('legacy') and _version(value['release']['tag'])<_version(state['installed']['tag']):
            raise ReleaseError('Use local rollback to select the previous program version.','DOWNGRADE_REFUSED')
        result={'schema':1,'operation':'apply','tag':value['release']['tag'],'current':state['installed'],
                'release':value['release'],'notes':value['notes'],'manifest_sha256':value['manifest_sha256'],
                'before_current':self._current(),'before_state':self._state(),
                'message':'Only program code and its private version environment change; reopen the console after success.'}
        result['fingerprint']=_digest(_encoded(result))
        return result

    def _check_plan(self,plan,operation):
        if not isinstance(plan,dict) or plan.get('operation')!=operation:
            raise ReleaseError('Invalid program operation preview.','TAMPERED_PLAN')
        raw=dict(plan);fingerprint=raw.pop('fingerprint',None)
        if fingerprint!=_digest(_encoded(raw)):raise ReleaseError('Program operation preview was modified.','TAMPERED_PLAN')

    def _registry(self):
        source=Path(__file__).with_name('root_actions.py')
        if not source.exists():
            current=self._current()
            if not current:return set()
            source=self.releases/current/'server_setting/root_actions.py'
        tree=ast.parse(source.read_text(encoding='utf-8'))
        for node in tree.body:
            if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='FINAL_STATUSES' for t in node.targets):
                value=node.value
                if (isinstance(value,ast.Call) and isinstance(value.func,ast.Name) and value.func.id=='frozenset'
                        and len(value.args)==1 and isinstance(value.args[0],ast.Set)
                        and all(isinstance(v,ast.Constant) and isinstance(v.value,str) for v in value.args[0].elts)):
                    return {v.value for v in value.args[0].elts}
        raise ReleaseError('Host transaction compatibility registry is unavailable.','UNSAFE_STATE')

    def _pending_host(self,ignore=None):
        if not self.transactions.exists():return
        self._trusted(self.transactions,True);terminal=self._registry()
        for child in self.transactions.iterdir():
            self._trusted(child,True)
            path=child/'journal.json'
            if path.is_symlink():raise ReleaseError('A transaction journal is an unexpected symbolic link.','UNSAFE_STATE')
            if not path.exists():
                # Only the transaction attested by our private durable intent
                # can be recovered between mkdir and its first shared write.
                if child.name==ignore:continue
                raise ReleaseError('An incomplete host transaction needs inspection.','RECOVERY_REQUIRED')
            value=_json(self._read(path))
            if value.get('transaction_id')!=child.name:raise ReleaseError('A host transaction identity is invalid.','UNSAFE_STATE')
            if child.name!=ignore and value.get('status') not in terminal:
                raise ReleaseError('An incomplete host transaction must be recovered before updating.','RECOVERY_REQUIRED')

    @contextmanager
    def _lock(self):
        self._dir(self.runtime,private=True)
        path=self.runtime/'transaction.lock'
        if path.exists() or path.is_symlink():self._trusted(path)
        fd=os.open(path,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
        try:
            if os.name=='posix':
                import fcntl
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise ReleaseError('Another host transaction is in progress.','BUSY') from None
            yield
        finally:os.close(fd)

    def _git_clean(self):
        if (self.root/'.git').exists() or (self.root/'.git').is_symlink():
            self._trusted(self.root/'.git',directory=(self.root/'.git').is_dir())
            result=self._checked(['git','-c','core.fsmonitor=false','-c','core.hooksPath=/dev/null','-C',str(self.root),'status','--porcelain'],timeout=8)
            if result.stdout.strip():raise ReleaseError('A dirty checkout will not be modified by the installer.','DIRTY_CHECKOUT')

    def _extract(self,raw,manifest,destination):
        if len(raw)!=manifest['archive']['size'] or _digest(raw)!=manifest['archive']['sha256']:
            raise ReleaseError('Release archive integrity check failed.','BAD_ARCHIVE_HASH')
        contents={};total=0
        try:
            with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as stream:
                members=stream.getmembers()
                if len(members)>4096:raise ReleaseError('Release archive has too many entries.','UNSAFE_ARCHIVE')
                for member in members:
                    path=PurePosixPath(member.name)
                    if member.isdir():
                        if member.name.rstrip('/')=='3waServerSetting':continue
                        # Directory records are unnecessary and can hide aliases.
                        raise ReleaseError('Release directories must be implicit.','UNSAFE_ARCHIVE')
                    if not member.isfile() or member.name!=path.as_posix() or not member.name.startswith('3waServerSetting/'):
                        raise ReleaseError('A nonregular archive object was refused.','UNSAFE_ARCHIVE')
                    relative=member.name[len('3waServerSetting/'):];_payload_path(relative)
                    if relative in contents or relative not in manifest['files']:raise ReleaseError('Release has an extra or duplicate member.','UNSAFE_ARCHIVE')
                    expected=manifest['files'][relative]
                    total+=member.size
                    if total>MAX_EXPANDED or member.size!=expected['size'] or member.mode!=expected['mode']:
                        raise ReleaseError('Release member size or mode disagrees with its manifest.','UNSAFE_ARCHIVE')
                    data=stream.extractfile(member).read(expected['size']+1)
                    if _digest(data)!=expected['sha256']:raise ReleaseError('Release member hash disagrees with its manifest.','BAD_ARCHIVE_HASH')
                    contents[relative]=data
        except ReleaseError:raise
        except (OSError,tarfile.TarError):raise ReleaseError('Release archive could not be safely read.','UNSAFE_ARCHIVE') from None
        if set(contents)!=set(manifest['files']):raise ReleaseError('Release archive omits declared files.','UNSAFE_ARCHIVE')
        identity=_json(contents['RELEASE.json'])
        for key in ('schema','product','repository','version','tag','commit','data_schema','python_requires','validated_platforms'):
            if identity.get(key)!=manifest[key]:raise ReleaseError('Payload identity differs from its signed manifest.','BAD_MANIFEST')
        if contents['release/allowed_signers']!=self._signers():raise ReleaseError('Release attempts to replace the pinned signer.','SIGNER_CHANGED')
        for relative,data in contents.items():
            target=destination/relative;self._dir(target.parent)
            target.write_bytes(data);target.chmod(manifest['files'][relative]['mode'])
        return contents

    def _make_venv(self,directory):
        self._checked(['python3','-I','-m','venv',str(directory/'.venv')],timeout=120,code='VENV_FAILED')
        self._checked([str(directory/'.venv/bin/python'),'-I','-m','pip','--isolated','--disable-pip-version-check','install',
            '--no-index','--find-links',str(directory/'wheelhouse'),'--require-hashes','--only-binary=:all:','--no-deps',
            '-r',str(directory/'requirements-tui.lock')],timeout=180,code='VENV_FAILED')
        # The bootstrap itself has umask077; this code-only environment must
        # remain readable for its unprivileged smoke and ordinary users.
        for current,dirs,files in os.walk(directory/'.venv',followlinks=False):
            Path(current).chmod(0o755)
            for name in files:
                path=Path(current)/name
                if not path.is_symlink():path.chmod(0o755 if path.stat().st_mode&0o111 else 0o644)

    def _smoke(self,directory):
        # Verified code is still tested without privilege before becoming current.
        self._checked(['runuser','--user','nobody','--',str(directory/'.venv/bin/python'),'-E','-s','-m','server_setting','--smoke'],
                      timeout=90,cwd=str(directory),code='SMOKE_FAILED')

    def _relocate_venv_scripts(self,directory,final):
        for path in (directory/'.venv/bin').iterdir():
            if path.is_symlink() or not path.is_file() or path.stat().st_size>65536:continue
            raw=path.read_bytes()
            if raw.startswith(b'#!') and str(directory).encode() in raw[:2048]:
                path.write_bytes(raw.replace(str(directory).encode(),str(final).encode()))

    def _durable_tree(self,directory):
        # Publish neither the version directory nor current before its payload,
        # generated environment and directory entries have reached storage.
        for current,dirs,files in os.walk(directory,topdown=False,followlinks=False):
            for name in files:
                path=Path(current)/name
                if path.is_symlink():continue
                self._trusted(path)
                descriptor=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
                try:os.fsync(descriptor)
                finally:os.close(descriptor)
            self._sync(Path(current))

    def _ready(self,directory,manifest):
        self._atomic(directory/'.release-ready.json',_encoded({'schema':1,
            'manifest_sha256':_digest(_encoded(manifest)),'smoke_verified':True}))

    def _prepare(self,raw,manifest,manifest_raw,signature):
        if not isinstance(raw,bytes) or len(raw)!=manifest['archive']['size'] or _digest(raw)!=manifest['archive']['sha256']:
            raise ReleaseError('Release archive integrity check failed.','BAD_ARCHIVE_HASH')
        name=manifest['version']+'-'+manifest['commit'][:12]
        final=self.releases/name
        if final.exists() or final.is_symlink():
            existing=self._installed_manifest(name,verify=True)
            if existing!=manifest:raise ReleaseError('An existing version directory has different content.','VERSION_CONFLICT')
            return name
        self._dir(self.releases)
        temporary=Path(tempfile.mkdtemp(prefix='.staging-',dir=self.releases))
        try:
            self._extract(raw,manifest,temporary)
            temporary.chmod(0o755)
            self._atomic(temporary/'.release-manifest.json',_encoded(manifest))
            self._atomic(temporary/'.signed-manifest.json',manifest_raw)
            self._atomic(temporary/'.signed-manifest.sig',signature)
            self._make_venv(temporary)
            self._smoke(temporary)
            self._relocate_venv_scripts(temporary,final)
            self._ready(temporary,manifest)
            self._durable_tree(temporary)
            os.replace(temporary,final)
            self._sync(self.releases)
            return name
        except BaseException:
            if temporary.exists():
                # Keep failed staging private and inspectable; never recursively
                # remove a computed tree from the update path.
                temporary.chmod(0o700)
            raise

    def _legacy(self,candidate):
        source=self.root/'server_setting'
        if not source.exists():return None
        if source.is_symlink():raise ReleaseError('An unowned program alias prevents first installation.','UNSAFE_PATH')
        self._trusted(source,True);self._git_clean()
        required=['run.sh','requirements-tui.lock','server_setting/__init__.py','server_setting/__main__.py']
        if any(not (self.root/p).is_file() for p in required):raise ReleaseError('Flat installation cannot be recognized safely.','UNRECOGNIZED_INSTALL')
        files={}
        paths=[]
        for path in source.iterdir():
            if path.name=='__pycache__' and path.is_dir() and not path.is_symlink():continue
            if path.suffix!='.py' or not path.is_file() or path.is_symlink():
                raise ReleaseError('Unknown package data must be preserved explicitly before adopting a flat install.','UNRECOGNIZED_INSTALL')
            paths.append(path)
        paths += [self.root/name for name in ('run.sh','README.md','LICENSE','nat.sh','firewall_plan.py','requirements-tui.txt','requirements-tui.lock') if (self.root/name).exists()]
        paths += list((self.root/'config').glob('*.example.*')) if (self.root/'config').is_dir() else []
        if (self.root/'tests/sqlite-smoke.php').exists():paths.append(self.root/'tests/sqlite-smoke.php')
        for path in paths:
            relative=path.relative_to(self.root).as_posix();_payload_path(relative)
            raw=self._read(path,MAX_EXPANDED);files[relative]=(raw,0o755 if relative.endswith('.sh') else 0o644)
        name='legacy-'+_digest(_encoded({p:_digest(raw) for p,(raw,_) in files.items()}))[:12]
        final=self.releases/name
        if final.exists():self._installed_manifest(name,verify=True);return name
        temporary=Path(tempfile.mkdtemp(prefix='.legacy-staging-',dir=self.releases))
        try:
            temporary.chmod(0o755)
            for relative,(raw,mode) in files.items():
                path=temporary/relative;self._dir(path.parent);path.write_bytes(raw);path.chmod(mode)
            info={'version':'legacy','tag':'legacy','commit':'unknown','data_schema':1,'validated_platforms':[],'legacy':True}
            provenance=self._read(self.root/'SOURCE_INFO.json')
            if provenance:
                value=_json(provenance);commit=value.get('base_commit')
                if isinstance(commit,str) and COMMIT.fullmatch(commit):info['commit']=commit
            info['files']={p:{'sha256':_digest(raw),'size':len(raw),'mode':mode} for p,(raw,mode) in files.items()}
            self._atomic(temporary/'.release-manifest.json',_encoded(info))
            # A failed legacy snapshot stays staging; it must never become an
            # apparently completed rollback target on the next installer run.
            shutil.copytree(self.releases/candidate/'wheelhouse',temporary/'wheelhouse')
            self._make_venv(temporary);self._smoke(temporary)
            self._relocate_venv_scripts(temporary,final)
            self._ready(temporary,info);self._durable_tree(temporary)
            os.replace(temporary,final);self._sync(self.releases)
        except BaseException:
            if temporary.exists():temporary.chmod(0o700)
            raise
        return name

    def _write_state(self,value):self._atomic(self.state_file,_encoded(value))

    def _write_journal(self,value):
        terminal=value['status'] in TERMINAL
        private=(self.journal_file,_encoded(value),0o600)
        public=(self.control/'pending.json',_encoded({'pending':
            {'schema':1,'transaction_id':value['transaction_id'],'status':value['status'],'recoverable':True}
            if not terminal else None}),0o644)
        directory=self.transactions/value['transaction_id']
        status='APPLIED' if value['status']=='UPDATED' else 'ROLLED_BACK' if terminal else 'UPDATE_SWITCHING'
        shared=(directory/'journal.json',_encoded({'schema':1,'transaction_id':value['transaction_id'],'module':'release',
            'kind':'release-update','status':status,'created_ns':value['created_ns'],'result':value['status']}),0o600)
        # Commit the private canonical terminal only after the shared gate and
        # public summary. Any earlier interruption remains recoverable.
        for record in ((shared,public,private) if terminal else (private,public,shared)):
            if record is shared:self._dir(directory,private=True)
            self._atomic(*record)

    def _point(self,name):
        link=self.root/'current'
        if name is None:
            if link.is_symlink():link.unlink();self._sync(self.root)
            elif link.exists():raise ReleaseError('Current pointer was externally replaced.','UNSAFE_PATH')
            return
        self._installed_manifest(name,verify=True)
        temporary=self.root/('.current-'+uuid.uuid4().hex)
        temporary.symlink_to('releases/'+name,target_is_directory=True)
        try:os.replace(temporary,link);self._sync(self.root)
        finally:
            if temporary.is_symlink():temporary.unlink()

    def _aliases(self,candidate,legacy):
        alias=self.root/'server_setting'
        if alias.is_symlink():
            if os.readlink(alias)!='current/server_setting':raise ReleaseError('The program compatibility alias was externally changed.','UNSAFE_PATH')
        elif alias.exists():
            if not legacy:raise ReleaseError('Unrecognized flat program directory.','UNRECOGNIZED_INSTALL')
            old=self.control/'legacy-flat-server_setting'
            if old.exists() or old.is_symlink():raise ReleaseError('A legacy directory backup already exists.','UNSAFE_PATH')
            self._trusted(alias,True);os.replace(alias,old)
            self._sync(self.root);self._sync(self.control)
            alias.symlink_to('current/server_setting',target_is_directory=True)
        else:alias.symlink_to('current/server_setting',target_is_directory=True)
        self._sync(self.root)
        launcher=self.releases/candidate/'run.sh'
        self._atomic(self.root/'run.sh',self._read(launcher),0o755)

    def _launcher(self):
        raw=self._read(self.root/'run.sh',65536)
        return {'content':base64.b64encode(raw).decode(),'sha256':_digest(raw)} if raw is not None else None

    def _check_layout(self,current,state):
        alias=self.root/'server_setting'
        if current:
            if not alias.is_symlink() or os.readlink(alias)!='current/server_setting':
                raise ReleaseError('The installed program alias was externally changed.','EXTERNAL_EDIT')
            launcher=self._launcher()
            expected=state.get('launcher_sha256') or self._installed_manifest(current)['files']['run.sh']['sha256']
            if launcher is None or launcher['sha256']!=expected:
                raise ReleaseError('The stable launcher was externally modified.','EXTERNAL_EDIT')
        elif alias.is_symlink() or ((self.root/'run.sh').exists() and not alias.is_dir()):
            raise ReleaseError('Unrecognized launcher or alias prevents initial installation.','UNRECOGNIZED_INSTALL')

    def _check_legacy_console(self,current):
        if current and not self._installed_manifest(current).get('legacy'):
            return
        expected=self.releases/current if current else self.root
        proc=Path('/proc')
        if not proc.is_dir():return
        for entry in proc.iterdir():
            if not entry.name.isdigit() or int(entry.name)==os.getpid():continue
            try:
                argv=(entry/'cmdline').read_bytes().split(b'\0')
                is_console=b'server_setting' in argv
                active_directory=(entry/'cwd').resolve() if is_console else None
            except (OSError,RuntimeError):
                continue
            if is_console and active_directory==expected.resolve():
                raise ReleaseError('Close the running legacy console before adopting or replacing it.','BUSY')

    def _activate(self,name,manifest):
        current=self._current();before=self._state();pending=self._journal()
        if pending and pending.get('status') not in TERMINAL:raise ReleaseError('Recover the incomplete program update first.','RECOVERY_REQUIRED')
        if current!=before['current']:
            raise ReleaseError('The installed pointer changed outside the updater.','EXTERNAL_EDIT')
        self._check_layout(current,before)
        self._check_legacy_console(current)
        if current==name:return {'status':'NO_CHANGE','installed':self._identity(manifest,name),'restart_required':False}
        legacy=self._legacy(name) if current is None else None
        prior=current or legacy
        if prior:self._installed_manifest(prior,verify=True)
        journal={'schema':1,'transaction_id':time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+uuid.uuid4().hex[:12],
            'created_ns':time.time_ns(),'status':'UPDATE_SWITCHING','before_current':current,'restore_current':prior,
            'target':name,'before_state':before,'legacy':legacy,'before_launcher':self._launcher(),
            'after_launcher_sha256':manifest['files']['run.sh']['sha256']}
        self._write_journal(journal)
        try:
            if current is None and legacy:self._point(legacy)
            self._aliases(name,legacy)
            self._point(name)
            self._write_state({'schema':1,'current':name,'previous':prior,'launcher_sha256':manifest['files']['run.sh']['sha256']})
            journal['status']='UPDATED';self._write_journal(journal)
        except Exception:
            self._recover(journal)
            raise
        return {'status':'UPDATED','installed':self._identity(manifest,name),'previous':self._identity(self._installed_manifest(prior),prior) if prior else None,
                'restart_required':True,'message':'Program version changed; reopen the console. Host settings were not changed.'}

    def _recover(self,journal):
        current=self._current()
        if current not in {journal['before_current'],journal['restore_current'],journal['target']}:
            raise ReleaseError('Current pointer was changed outside the pending transaction.','RECOVERY_REQUIRED')
        restore=journal['restore_current']
        before_launcher=journal.get('before_launcher')
        current_launcher=self._launcher()
        permitted={journal.get('after_launcher_sha256'),before_launcher['sha256'] if before_launcher else None}
        if (current_launcher['sha256'] if current_launcher else None) not in permitted:
            raise ReleaseError('The stable launcher changed during recovery.','RECOVERY_REQUIRED')
        self._point(restore)
        if journal.get('legacy'):
            # Finish or repair the compatibility link even if power was lost
            # immediately after the original flat directory was renamed. Keep
            # the verified stable launcher so legacy uses its snapshot venv.
            self._aliases(journal['target'],journal['legacy'])
        elif before_launcher:
            self._atomic(self.root/'run.sh',base64.b64decode(before_launcher['content']),0o755)
        elif (self.root/'run.sh').exists():
            (self.root/'run.sh').unlink()
        if restore is None:
            alias=self.root/'server_setting'
            if alias.is_symlink() and os.readlink(alias)=='current/server_setting':alias.unlink()
            self._sync(self.root)
        before=journal['before_state']
        if journal.get('legacy') and before['current'] is None:
            before={'schema':1,'current':restore,'previous':None,'launcher_sha256':journal['after_launcher_sha256']}
        self._write_state(before)
        journal['status']='UPDATE_ROLLED_BACK';self._write_journal(journal)

    def install_local(self,archive,manifest,signature,signers):
        self._gate();self._active_code(allow_pending=True)
        raw=self._read(Path(manifest));sig=self._read(Path(signature),65536);trusted=self._signers(signers)
        self._verify_signature(raw,sig,trusted);value=validate_manifest(raw)
        package=self._read(Path(archive),MAX_ARCHIVE)
        self._dir(self.root);self._dir(self.control)
        with self._lock():
            self._active_code(allow_pending=True)
            journal=self._journal()
            if journal and journal.get('status') not in TERMINAL:
                self._pending_host(journal['transaction_id']);self._recover(journal)
            self._pending_host()
            if not self._current():self._git_clean()
            self._atomic(self.control/'allowed_signers',trusted)
            name=self._prepare(package,value,raw,sig)
            return self._activate(name,value)

    def apply(self,plan):
        self._gate();self._active_code();self._check_plan(plan,'apply')
        current=self.plan(plan.get('tag'))
        if current['fingerprint']!=plan['fingerprint']:raise ReleaseError('Program update preview is stale.','STALE_PLAN')
        remote=self._remote(plan['tag'])
        if _digest(remote['raw'])!=plan['manifest_sha256']:raise ReleaseError('Signed release changed after preview.','STALE_PLAN')
        raw=self._fetch(remote['archive_url'],MAX_ARCHIVE)
        with self._lock():
            self._active_code()
            self._pending_host()
            if self._current()!=plan['before_current'] or self._state()!=plan['before_state']:
                raise ReleaseError('Installed version changed after preview.','STALE_PLAN')
            pending=self._journal()
            if pending and pending['status'] not in TERMINAL:raise ReleaseError('Recover the interrupted update first.','RECOVERY_REQUIRED')
            name=self._prepare(raw,remote['manifest'],remote['raw'],remote['signature'])
            return self._activate(name,remote['manifest'])

    def rollback_plan(self):
        state=self._state();journal=self._journal();current=self._current()
        recovery=bool(journal and journal.get('status') not in TERMINAL)
        if current!=state['current'] and not recovery:
            raise ReleaseError('The installed pointer changed outside the updater.','EXTERNAL_EDIT')
        target=journal['restore_current'] if recovery else state.get('previous')
        if target is None and not recovery:raise ReleaseError('No previous local program version is available.','NO_PREVIOUS_RELEASE')
        identity=self._identity(self._installed_manifest(target,verify=True),target) if target else None
        result={'schema':1,'operation':'rollback','current':self._identity(self._installed_manifest(current),current) if current else None,
                'target':identity,'recovery':recovery,'before_current':current,'before_state':state,
                'transaction_id':journal['transaction_id'] if recovery else None,
                'message':'Only the previous program and its environment return; host settings and data are not rolled back.'}
        result['fingerprint']=_digest(_encoded(result));return result

    def rollback(self,plan):
        self._gate();self._active_code(allow_pending=True);self._check_plan(plan,'rollback')
        with self._lock():
            self._active_code(allow_pending=True)
            current=self.rollback_plan()
            if current['fingerprint']!=plan['fingerprint']:raise ReleaseError('Program rollback preview is stale.','STALE_PLAN')
            self._pending_host(plan['transaction_id'] if plan['recovery'] else None)
            if plan['recovery']:
                journal=self._journal();self._recover(journal)
            else:
                before=self._state();target=plan['target']['directory']
                self._check_layout(before['current'],before)
                journal={'schema':1,'transaction_id':time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+uuid.uuid4().hex[:12],
                    'created_ns':time.time_ns(),'status':'UPDATE_SWITCHING','before_current':before['current'],
                    'restore_current':before['current'],'target':target,'before_state':before,'legacy':None,
                    'before_launcher':self._launcher(),'after_launcher_sha256':before.get('launcher_sha256')}
                self._write_journal(journal)
                try:
                    self._point(target)
                    self._write_state({'schema':1,'current':target,'previous':before['current'],'launcher_sha256':before.get('launcher_sha256')})
                    journal['status']='UPDATE_ROLLED_BACK';self._write_journal(journal)
                except Exception:self._recover(journal);raise
            installed=self.status()['installed']
            return {'status':'UPDATE_ROLLED_BACK','installed':installed,'restart_required':True,'message':'Previous program restored; reopen the console. Host settings were preserved.'}


def main(argv=None):
    parser=argparse.ArgumentParser(description='Verified local release installer')
    parser.add_argument('--install-local',metavar='ARCHIVE',required=True)
    for option in ('manifest','signature','signers'):parser.add_argument('--'+option,required=True)
    parser.add_argument('--root',default=str(DEFAULT_ROOT));parser.add_argument('--confirm',action='store_true')
    args=parser.parse_args(argv)
    if not args.confirm:
        print(json.dumps({'status':'FAILED','code':'CONFIRMATION_REQUIRED','message':'Confirm explicit program installation.'}));return 2
    try:
        result=ReleaseManager(args.root).install_local(args.install_local,args.manifest,args.signature,args.signers)
        print(json.dumps(result,ensure_ascii=True));return 0
    except (ReleaseError,OSError) as error:
        print(json.dumps({'status':'FAILED','code':getattr(error,'code','INSTALL_FAILED'),
                          'message':str(error) if isinstance(error,ReleaseError) else 'Release filesystem operation failed.'}));return 1


if __name__=='__main__':raise SystemExit(main())

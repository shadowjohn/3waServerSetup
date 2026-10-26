"""Embedded installer verifier: authenticate every byte before executing payload code."""
import argparse
from datetime import date
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile


REPO = 'shadowjohn/3waServerSetup'
PRODUCT = '3waServerSetting'
NAMESPACE = '3wa-release@3wa.tw'
PRINCIPAL = '3waServerSetup-release'
MAX_ARCHIVE = 64*1024*1024
MAX_PAYLOAD = 128*1024*1024
TAG = re.compile(r'v(([0-9]{4})\.([0-9]{2})\.([0-9]{2})\.([0-9]{3})(?:-rc\.[1-9][0-9]*)?)')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def matches(pattern,value):
    return isinstance(value,str) and re.fullmatch(pattern,value)


def release_version(value):
    match = TAG.fullmatch(value) if isinstance(value,str) else None
    if not match or match[5]=='000':
        raise ValueError('Release tag must be vYYYY.MM.DD.NNN with sequence 001..999.')
    try:
        date(int(match[2]),int(match[3]),int(match[4]))
    except ValueError:
        raise ValueError('Release tag contains an invalid calendar date.') from None
    return match[1]


def read(path, maximum):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError('A bootstrap input is missing, oversized or a symlink.')
    with path.open('rb') as stream:
        data = stream.read(maximum+1)
    if len(data) > maximum:
        raise ValueError('A bootstrap input exceeded its bound.')
    return data


def unique(pairs):
    result = {}
    for key,value in pairs:
        if key in result:
            raise ValueError('Duplicate release metadata field.')
        result[key] = value
    return result


def safe_path(name):
    if (not isinstance(name,str) or not name or len(name)>512 or '\\' in name or '\x00' in name
            or name.startswith('/') or any(p in {'','.', '..'} for p in name.split('/'))):
        raise ValueError('An archive pathname is unsafe.')
    if name.split('/')[0] in {'.git','.root-dev','.venv','backups','modules','transactions','logs'}:
        raise ValueError('Private runtime data is not a release payload.')
    return name


def command(name):
    path = shutil.which(name, path='/usr/sbin:/usr/bin:/sbin:/bin' if sys.platform=='linux' else None)
    if not path or '.sbx-denybin' in path.lower():
        raise ValueError('A trusted OpenSSH verifier is required.')
    resolved = Path(path).resolve()
    if sys.platform=='linux' and os.geteuid()==0:
        for index,item in enumerate([resolved,*resolved.parents]):
            info = item.stat()
            if info.st_uid!=0 or info.st_mode&0o022 or not (stat.S_ISREG(info.st_mode) if index==0 else stat.S_ISDIR(info.st_mode)):
                raise ValueError('The verifier is not root controlled.')
    return path


def verify_manifest(directory, installer, signers, requested_tag=None):
    directory = Path(directory)
    raw = read(directory/'release-manifest.json',1024*1024)
    signature = directory/'release-manifest.json.sig'
    read(signature,16384); read(signers,8192)
    env = {'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','HOME':str(directory),'LANG':'C','LC_ALL':'C'} if sys.platform=='linux' else dict(os.environ)
    try:
        result = subprocess.run([command('ssh-keygen'),'-Y','verify','-f',str(signers),'-I',PRINCIPAL,
            '-n',NAMESPACE,'-s',str(signature)],input=raw,capture_output=True,timeout=15,env=env)
    except (OSError,subprocess.TimeoutExpired):
        raise ValueError('Release signature verification could not complete.') from None
    if result.returncode:
        raise ValueError('The release manifest signature is not from the pinned publisher.')
    try:
        value = json.loads(raw,object_pairs_hook=unique)
    except (UnicodeError,RecursionError,json.JSONDecodeError):
        raise ValueError('Release metadata is invalid.') from None
    if not isinstance(value,dict):
        raise ValueError('Release metadata must be an object.')
    parsed_version = release_version(value.get('tag'))
    if (value.get('version')!=parsed_version or type(value.get('schema')) is not int or value['schema']!=1
            or value.get('product')!=PRODUCT or value.get('repository')!=REPO
            or type(value.get('data_schema')) is not int or value['data_schema']!=1
            or value.get('python_requires') not in ('>=3.12','>=3.10')
            or not matches(r'[0-9a-f]{40}',value.get('commit'))):
        raise ValueError('Release identity is invalid or incompatible.')
    if requested_tag and requested_tag != value['tag']:
        raise ValueError('The downloaded release differs from the requested tag.')
    if not requested_tag and '-rc.' in value['tag']:
        raise ValueError('The stable installer does not select a prerelease.')
    platforms = value.get('validated_platforms')
    if (not isinstance(platforms,list) or not platforms
            or any(not isinstance(p,str) or p not in {'ubuntu22.04','ubuntu24.04','ubuntu26.04'} for p in platforms)
            or len(set(platforms))!=len(platforms)):
        raise ValueError('Unknown validated platform metadata.')
    expected_archive = PRODUCT+'-'+value['tag']+'.tar.gz'
    for kind,name,limit in (('archive',expected_archive,MAX_ARCHIVE),('installer','install.sh',1024*1024)):
        asset = value.get(kind)
        if (not isinstance(asset,dict) or asset.get('name')!=name or type(asset.get('size')) is not int
                or not 0<asset['size']<=limit or not matches(r'[0-9a-f]{64}',asset.get('sha256'))):
            raise ValueError('Release asset metadata is invalid.')
    actual_installer = read(installer,1024*1024)
    if len(actual_installer)!=value['installer']['size'] or sha(actual_installer)!=value['installer']['sha256']:
        raise ValueError('The running installer differs from the signed release.')
    files = value.get('files')
    if not isinstance(files,dict) or not 1<=len(files)<=2048:
        raise ValueError('The release file manifest is missing or too large.')
    total = 0
    for name,entry in files.items():
        safe_path(name)
        if (not isinstance(entry,dict) or type(entry.get('size')) is not int or not 0<=entry['size']<=MAX_PAYLOAD
                or type(entry.get('mode')) is not int or entry['mode'] not in {0o644,0o755}
                or not matches(r'[0-9a-f]{64}',entry.get('sha256'))):
            raise ValueError('An installed file has invalid metadata.')
        total += entry['size']
    if total>MAX_PAYLOAD or not {'RELEASE.json','run.sh','server_setting/release_update.py','release/allowed_signers','requirements-tui.lock'}<=set(files):
        raise ValueError('The release payload is incomplete or too large.')
    minimum=(3,10) if value['python_requires']=='>=3.10' else (3,12)
    if sys.version_info[:2]<minimum:
        raise ValueError(f'Selected release requires Python {minimum[0]}.{minimum[1]} or newer.')
    return value


def extract_payload(archive_path, manifest, destination, signers):
    raw = read(archive_path,MAX_ARCHIVE)
    if len(raw)!=manifest['archive']['size'] or sha(raw)!=manifest['archive']['sha256']:
        raise ValueError('The archive differs from the signed manifest.')
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw),mode='rb') as compressed:
            expanded = compressed.read(MAX_PAYLOAD+4*1024*1024+1)
        if len(expanded)>MAX_PAYLOAD+4*1024*1024:
            raise ValueError('The decompressed archive exceeds its bound.')
        contents = {}
        with tarfile.open(fileobj=io.BytesIO(expanded),mode='r:') as archive:
            for item in archive:
                if len(contents)>=2048 or not item.isfile() or not item.name.startswith(PRODUCT+'/'):
                    raise ValueError('The archive contains an unsupported file type or prefix.')
                name = safe_path(item.name[len(PRODUCT)+1:])
                if name in contents or name not in manifest['files']:
                    raise ValueError('The archive contains a duplicate or undeclared file.')
                expected = manifest['files'][name]
                if item.size!=expected['size'] or item.mode!=expected['mode'] or item.uid!=0 or item.gid!=0:
                    raise ValueError('The archive file metadata differs from the manifest.')
                stream = archive.extractfile(item)
                content = stream.read(expected['size']+1)
                if len(content)!=expected['size'] or sha(content)!=expected['sha256']:
                    raise ValueError('An archive file differs from its signed hash.')
                contents[name] = content
    except (OSError,tarfile.TarError,EOFError):
        raise ValueError('The release archive could not be validated.') from None
    if set(contents)!=set(manifest['files']):
        raise ValueError('The archive is missing declared files.')
    if contents['release/allowed_signers']!=read(signers,8192).replace(b'\r\n',b'\n'):
        raise ValueError('A release cannot silently replace its trusted signer.')
    try:
        identity = json.loads(contents['RELEASE.json'],object_pairs_hook=unique)
    except (UnicodeError,json.JSONDecodeError):
        raise ValueError('The payload identity is invalid.') from None
    if not isinstance(identity,dict):
        raise ValueError('The payload identity must be an object.')
    for key in ('schema','product','repository','version','tag','commit','data_schema','python_requires','validated_platforms'):
        if identity.get(key)!=manifest[key]:
            raise ValueError('The payload identity differs from its signed release.')
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError('Bootstrap extraction requires a new private directory.')
    destination.mkdir(mode=0o700)
    root = destination/PRODUCT; root.mkdir(mode=0o700)
    for name,content in sorted(contents.items()):
        path = root/Path(*PurePosixPath(name).parts)
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        descriptor = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),manifest['files'][name]['mode'])
        with os.fdopen(descriptor,'wb') as stream:
            stream.write(content)
        path.chmod(manifest['files'][name]['mode'])
    return root/'server_setting/release_update.py'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('manifest','install'))
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--installer',type=Path,required=True)
    parser.add_argument('--signers',type=Path,required=True)
    parser.add_argument('--release')
    parser.add_argument('--root',type=Path,default=Path('/opt/3waServerSetting'))
    args = parser.parse_args(argv)
    try:
        manifest = verify_manifest(args.directory,args.installer,args.signers,args.release)
        if args.operation=='manifest':
            print(manifest['archive']['name'])
            return 0
        if not args.root.is_absolute():
            raise ValueError('The installation root must be absolute.')
        archive = args.directory/manifest['archive']['name']
        entry = extract_payload(archive,manifest,args.directory/'payload',args.signers)
        result = subprocess.run([sys.executable,'-I',str(entry),'--install-local',str(archive),
            '--manifest',str(args.directory/'release-manifest.json'),'--signature',str(args.directory/'release-manifest.json.sig'),
            '--signers',str(args.signers),'--root',str(args.root),'--confirm'],stdin=subprocess.DEVNULL,check=False)
        return result.returncode
    except (ValueError,OSError) as error:
        print('Installation refused: '+str(error),file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())

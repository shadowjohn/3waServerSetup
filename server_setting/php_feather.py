"""Reviewed custom PDO SQLite activation; distribution modules stay untouched."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .root_actions import RootActionError, RootManager, BACKUPS, _fingerprint

STATE='/var/lib/3wa-server-setup/php-feather'
ACTIVE=STATE+'/active.json'
DIAGNOSTICS='/var/lib/3wa-server-setup/feather-build/diagnostics'
GATE='pdo_sqlite.feather_sql_load_extension'
MARKER='; Managed by 3waServerSetup Feather SQLite'
SCOPES=('cli','fpm')


def _sha(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _stock(data):
    try:
        lines=[line.strip() for line in data.decode('utf-8').splitlines()
               if line.strip() and not line.lstrip().startswith(';')]
    except UnicodeError:return False
    return len(lines)==1 and re.fullmatch(r'extension\s*=\s*["\']?pdo_sqlite[.]so["\']?',lines[0]) is not None


class FeatherSQLiteManager:
    def __init__(self,host=None,builder=None):
        if builder is None:
            from .php_feather_build import FeatherBuilder
            builder=FeatherBuilder(host)
        self.builder=builder
        self.host=host or builder.host
        self.core=RootManager(self.host)

    def _gate(self):
        self.core._gate('php')

    def _read_json(self,path):
        target=self.host.path(path)
        if not target.exists():return None
        self.core._trusted_file(target)
        if target.stat().st_size>2*1024*1024:raise RootActionError('Feather state exceeds its bound.','FEATHER_UNSUPPORTED')
        value=json.loads(target.read_text())
        if not isinstance(value,dict):raise RootActionError('Invalid private Feather state.','FEATHER_UNSUPPORTED')
        return value

    def _active(self):
        value=self._read_json(ACTIVE)
        if value is not None and (value.get('schema')!=1 or not isinstance(value.get('scopes'),dict)
                or set(value['scopes'])-set(SCOPES) or not isinstance(value.get('stock_aliases'),dict)):
            raise RootActionError('Invalid Feather activation manifest.','FEATHER_UNSUPPORTED')
        return value

    def _native(self):
        # A stock restore must not depend on development packages still being
        # installable. Native inspection also avoids repeated APT simulations.
        value=({'supported':True,'native':self.host.inspect()} if hasattr(self.host,'inspect') else self.builder.status())
        if not value.get('supported'):
            raise RootActionError('A matching native PHP build profile is unavailable.','FEATHER_NOT_READY')
        native=dict(value.get('native',value))
        if not all(key in native for key in ('php_version','package_version','abi','arch','nts')):
            raise RootActionError('Incomplete native PHP build identity.','FEATHER_NOT_READY')
        if not str(native['php_version']).startswith('8.5.') or native['nts'] is not True or not re.fullmatch(r'[0-9]{8}',str(native['abi'])):
            raise RootActionError('Only the reviewed native PHP 8.5 NTS profile is supported.','FEATHER_UNSUPPORTED')
        return native

    def _fpm_target(self):
        from .root_recipes import _fpm
        return _fpm(self.host)

    def _service(self,target):
        output=self._checked(['systemctl','show',target['unit'],'--property=LoadState','--property=ActiveState','--property=UnitFileState'])
        values=dict(line.split('=',1) for line in output.splitlines() if '=' in line)
        if (values.get('LoadState')!='loaded' or values.get('ActiveState') not in {'active','inactive'}
                or values.get('UnitFileState') not in {'enabled','disabled'}):
            raise RootActionError('The FPM service state is not exactly reversible.','FEATHER_UNSUPPORTED')
        return {'unit':target['unit'],'active':values['ActiveState']=='active','enabled':values['UnitFileState']=='enabled'}

    def _checked(self,argv,timeout=30):
        result=self.host.run(argv,timeout=timeout)
        if result.returncode:raise RootActionError('A checked Feather PHP operation failed.','FEATHER_NOT_READY')
        return result.stdout

    def _ini(self,entry,version):
        """Read only a native same-version mods-available alias or regular ini."""
        parent=self.host.path('/etc/php/'+version+'/'+entry.parent.parent.name+'/conf.d')
        self.core._trusted_target(parent/'__feather_parent_check__')
        if entry.is_symlink():
            info=entry.lstat()
            if os.name=='posix' and info.st_uid!=0:raise RootActionError('A PHP module alias is not root owned.','FEATHER_UNSUPPORTED')
            target=entry.readlink().as_posix()
            match=re.fullmatch(r'(?:\.\./\.\./mods-available/|/etc/php/'+re.escape(version)+r'/mods-available/)([A-Za-z0-9_.-]+[.]ini)',target)
            if not match:raise RootActionError('Only native same-version PHP module aliases are supported.','FEATHER_UNSUPPORTED')
            canonical=self.host.path('/etc/php/'+version+'/mods-available/'+match[1])
        else:canonical=entry
        self.core._metadata(canonical)
        if not canonical.is_file() or canonical.stat().st_size>1024*1024:
            raise RootActionError('A native PHP ini input is missing or too large.','FEATHER_NOT_READY')
        return canonical.read_bytes()

    def _alias_path(self,scope,version='8.5'):
        parent=self.host.path(f'/etc/php/{version}/{scope}/conf.d')
        self.core._trusted_target(parent/'__feather_parent_check__')
        return parent/'20-pdo_sqlite.ini'

    def _alias(self,scope,active,allow_transition=False):
        path=self._alias_path(scope)
        if not path.exists() and not path.is_symlink():
            raise RootActionError('Install the distribution SQLite package before custom activation.','FEATHER_NOT_READY')
        info=path.lstat();data=self._ini(path,'8.5')
        link=path.is_symlink()
        if link and path.readlink().as_posix() not in {
                '../../mods-available/pdo_sqlite.ini','/etc/php/8.5/mods-available/pdo_sqlite.ini'}:
            raise RootActionError('The PDO SQLite alias is not the native stock link.','FEATHER_UNSUPPORTED')
        record=(active or {}).get('scopes',{}).get(scope)
        managed=not link and record is not None and data==self._content(record['artifact'])
        previous=(record or {}).get('previous_alias')
        if (not link and previous and previous.get('kind')=='regular'
                and previous.get('sha256')==_sha(data)):
            managed=True
        if not managed and not _stock(data) and not allow_transition:
            raise RootActionError('A custom or externally changed PDO SQLite ini must be inspected first.','FEATHER_UNSUPPORTED')
        attrs={}
        if hasattr(os,'listxattr'):
            try:
                attrs={name:base64.b64encode(os.getxattr(path,name,follow_symlinks=False)).decode()
                       for name in os.listxattr(path,follow_symlinks=False)}
            except OSError:raise RootActionError('Cannot preserve the native PHP alias metadata.','FEATHER_UNSUPPORTED') from None
        return {'path':f'/etc/php/8.5/{scope}/conf.d/20-pdo_sqlite.ini','kind':'symlink' if link else 'regular',
            'target':path.readlink().as_posix() if link else None,'sha256':_sha(data),'inode':info.st_ino,'device':info.st_dev,
            'metadata':{'mode':stat.S_IMODE(info.st_mode),'uid':info.st_uid,'gid':info.st_gid,'xattrs':attrs},
            'content':base64.b64encode(data).decode() if not link else None,'managed':managed}

    def _scan(self):
        from .root_recipes import _no_fpm_bootstrap
        inventory={}
        for scope in SCOPES:
            ini=f'/etc/php/8.5/{scope}/php.ini'
            data=self.host.read(ini)
            if data is None:raise RootActionError('The native PHP ini is unavailable.','FEATHER_NOT_READY')
            self.core._metadata(self.host.path(ini));_no_fpm_bootstrap(data)
            if re.search(r'^\s*(?:extension\s*=.*pdo_sqlite|'+re.escape(GATE)+r'\s*=)',data,re.M|re.I):
                raise RootActionError('A PDO SQLite override exists outside its native alias.','FEATHER_UNSUPPORTED')
            inventory[ini]=_sha(data.encode())
            directory=self.host.path(f'/etc/php/8.5/{scope}/conf.d')
            for entry in sorted(directory.glob('*.ini')):
                content=self._ini(entry,'8.5')
                text=content.decode('utf-8')
                _no_fpm_bootstrap(text)
                if entry.name!='20-pdo_sqlite.ini' and re.search(
                        r'^\s*(?:extension\s*=.*pdo_sqlite|'+re.escape(GATE)+r'\s*=)',text,re.M|re.I):
                    raise RootActionError('Conflicting PDO SQLite ini entries are unsupported.','FEATHER_UNSUPPORTED')
                inventory[f'/etc/php/8.5/{scope}/conf.d/{entry.name}']=_sha(content)
        for path in [self.host.path('/etc/php/8.5/fpm/php-fpm.conf'),
                     *sorted(self.host.path('/etc/php/8.5/fpm/pool.d').glob('*.conf'))]:
            self.core._metadata(path)
            if not path.is_file():raise RootActionError('Native FPM configuration is unavailable.','FEATHER_NOT_READY')
            inventory['/'+path.relative_to(self.host.root).as_posix()]=_sha(path.read_bytes())
        return inventory

    def _profile(self,strict_artifacts=True,transition_scopes=()):
        self._gate();native=self._native();target=self._fpm_target()
        versions=[path.name for path in self.host.path('/etc/php').iterdir()
                  if re.fullmatch(r'[0-9]+[.][0-9]+',path.name)
                  and any((path/scope/'php.ini').is_file() for scope in SCOPES)]
        if versions!=['8.5']:
            raise RootActionError('Multiple PHP versions require explicit separate support.','FEATHER_UNSUPPORTED')
        if target.get('version')!='8.5' or target.get('pool')!='www':
            raise RootActionError('Only one native PHP 8.5 www pool is supported.','FEATHER_UNSUPPORTED')
        active=self._active()
        aliases={scope:self._alias(scope,active,scope in transition_scopes) for scope in SCOPES}
        if strict_artifacts and active:
            for record in active['scopes'].values():self._validate_artifact(record['artifact'])
        stock=self.host.path('/usr/lib/php/'+str(native['abi'])+'/pdo_sqlite.so')
        self.core._metadata(stock)
        if not stock.is_file():raise RootActionError('The official PDO SQLite module is missing.','FEATHER_NOT_READY')
        return {'native':native,'aliases':{scope:{key:value for key,value in item.items() if key!='content'} for scope,item in aliases.items()},
            'service':self._service(target),'target':target,'inputs':self._scan(),
            'stock_module_sha256':_sha(stock.read_bytes()),'active_sha256':_sha(self.host.path(ACTIVE).read_bytes()) if active else None}

    def _validate_artifact(self,artifact):
        if hasattr(self.builder,'validate_artifact'):result=self.builder.validate_artifact(artifact,host=self.host)
        else:
            from .php_feather_build import validate_artifact
            result=validate_artifact(artifact,host=self.host)
        if not isinstance(result,dict):raise RootActionError('The compiled artifact is unavailable.','FEATHER_NOT_READY')
        for path_key,hash_key in (('module_path','sha256'),('probe_so_path','probe_sha256')):
            path=result[path_key]
            if not isinstance(path,str) or not path.startswith('/opt/3waServerSetting/modules/'):
                raise RootActionError('A compiled artifact path is outside managed storage.','FEATHER_UNSUPPORTED')
            target=self.host.path(path);self.core._metadata(target)
            if not target.is_file() or _sha(target.read_bytes())!=result[hash_key]:
                raise RootActionError('Compiled artifact integrity failed.','FEATHER_NOT_READY')
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}',result['probe_function']):
            raise RootActionError('Invalid compiled probe identity.','FEATHER_UNSUPPORTED')
        return result

    @staticmethod
    def _content(artifact):
        path=artifact['module_path']
        if any(char in path for char in '\n\r\0"'):raise RootActionError('Invalid module artifact path.','FEATHER_UNSUPPORTED')
        return (MARKER+'\nextension="'+path+'"\n'+GATE+'=On\n').encode()

    def _pending(self):
        return [item for item in self.core._journals() if item.get('kind')=='php-feather'
                and item.get('status') not in {'FEATHER_APPLIED','FEATHER_ROLLED_BACK','FEATHER_FAILED'}]

    def status(self):
        pending=None
        try:
            own=self._pending()
            pending={'transaction_id':own[-1]['transaction_id'],'status':own[-1]['status']} if own else None
            profile=self._profile();active=self._active()
            return {'supported':True,'detail':'Official module preserved; custom SQL loading is enabled only for explicitly selected SAPIs.',
                **profile['native'],'cli_enabled':profile['aliases']['cli']['managed'],'fpm_enabled':profile['aliases']['fpm']['managed'],
                'stock_available':True,'artifact':({scope:value['artifact'] for scope,value in active['scopes'].items()} if active else None),'pending':pending}
        except Exception as error:
            return {'supported':False,'detail':str(error) if isinstance(error,(RootActionError,ValueError)) else 'Feather PHP state is unavailable.',
                'php_version':None,'abi':None,'cli_enabled':None,'fpm_enabled':None,'stock_available':False,'artifact':None,'pending':pending}

    def plan(self,cli=True,fpm=True):
        if type(cli) is not bool or type(fpm) is not bool or not (cli or fpm):
            raise RootActionError('Select at least one explicit CLI/FPM activation scope.','FEATHER_UNSUPPORTED')
        before=self._profile()
        result={'schema':1,'module':'php-feather','cli':cli,'fpm':fpm,'build_plan':self.builder.plan(),'before':before,
            'notes':['Build matching Ubuntu PDO SQLite with a root-controlled default-Off SQL loading gate.',
                     'Selected SAPIs enable the custom module and gate; unselected SAPIs remain unchanged.',
                     'The official system .so is never overwritten. Installed build dependencies are not rolled back.',
                     'FPM configuration is tested and its service reloaded; a local Unix FastCGI SQL extension probe must pass.',
                     'Restore stock retains the first official alias baseline across later custom rebuilds.']}
        result['fingerprint']=_fingerprint(result);return result

    def _replace_alias(self,scope,snapshot,expected):
        path=self._alias_path(scope)
        current=self._alias(scope,self._active(),True)
        if self._signature(current)!=self._signature(expected):
            raise RootActionError('A PHP alias changed outside the reviewed operation.','STALE_PLAN')
        metadata=snapshot['metadata']
        fd,name=tempfile.mkstemp(prefix='.3wa-feather-',dir=path.parent)
        temporary=Path(name)
        try:
            if snapshot['kind']=='symlink':
                os.close(fd);temporary.unlink()
                if snapshot['target'] not in {'../../mods-available/pdo_sqlite.ini','/etc/php/8.5/mods-available/pdo_sqlite.ini'}:
                    raise RootActionError('Invalid stock alias recovery target.','FEATHER_UNSUPPORTED')
                temporary.symlink_to(snapshot['target'])
                if hasattr(os,'lchown'):os.lchown(temporary,metadata['uid'],metadata['gid'])
            else:
                with os.fdopen(fd,'wb') as stream:
                    stream.write(base64.b64decode(snapshot['content']));stream.flush();os.fsync(stream.fileno())
                    if os.name=='posix':os.fchown(stream.fileno(),metadata['uid'],metadata['gid'])
                    os.chmod(temporary,metadata['mode'])
            for key,value in metadata.get('xattrs',{}).items():
                os.setxattr(temporary,key,base64.b64decode(value),follow_symlinks=False)
            if self._signature(self._alias(scope,self._active(),True))!=self._signature(expected):
                raise RootActionError('A PHP alias changed before replacement.','STALE_PLAN')
            os.replace(temporary,path)
            if os.name=='posix':
                descriptor=os.open(path.parent,os.O_DIRECTORY)
                try:os.fsync(descriptor)
                finally:os.close(descriptor)
        finally:
            if temporary.exists() or temporary.is_symlink():temporary.unlink()

    @staticmethod
    def _signature(value):
        return {key:value.get(key) for key in ('kind','target','sha256')}

    def _write_active(self,value):
        if value is None:
            path=self.host.path(ACTIVE)
            if path.exists():self.core._trusted_file(path);path.unlink()
        else:self.core._private_json(ACTIVE,value)

    def _fpm_reload(self,target,service):
        self._checked([target['binary'],'-t','--fpm-config','/etc/php/8.5/fpm/php-fpm.conf'])
        self._checked(['systemctl','reload-or-restart' if service['active'] else 'start',target['unit']])

    def _probe(self,scope,artifact,enabled,target):
        from .root_fpm import _request
        payload=base64.b64encode(json.dumps({'path':artifact['probe_so_path'],'function':artifact['probe_function']}).encode()).decode()
        source=('''<?php
header('Content-Type: application/json');
$c=json_decode(base64_decode("PAYLOAD"),true);
$db=new PDO('sqlite::memory:');$db->setAttribute(PDO::ATTR_ERRMODE,PDO::ERRMODE_EXCEPTION);
$loaded=false;$rejected=false;$value=null;
try { $db->query("SELECT load_extension(".$db->quote($c['path']).")");$loaded=true;
 $value=$db->query("SELECT ".$c['function']."()")->fetchColumn();
} catch(Throwable $e) {
 if(str_contains(strtolower($e->getMessage()),'not authorized'))$rejected=true;
 else { echo json_encode(['sapi'=>PHP_SAPI,'values'=>['error'=>'SQL_PROBE_FAILED']]);exit; }
}
$fresh=new PDO('sqlite::memory:');$isolated=false;
try{$fresh->query("SELECT ".$c['function']."()");}catch(Throwable $e){$isolated=true;}
$gate=in_array(strtolower((string)ini_get('pdo_sqlite.feather_sql_load_extension')),['1','on','yes','true'],true);
echo json_encode(['sapi'=>PHP_SAPI,'values'=>['gate'=>$gate,'sql_load_extension'=>$loaded,
 'authorized_rejection'=>$rejected,'probe_value'=>$value,'fresh_connection_unloaded'=>$isolated]]);
'''.replace('PAYLOAD',payload)).encode()
        directory=Path(tempfile.mkdtemp(prefix='3wa-feather-',dir=self.host.path('/run')))
        path=directory/'probe.php'
        try:
            directory.chmod(0o711);path.write_bytes(source);path.chmod(0o644)
            if scope=='cli':
                output=self._checked(['php8.5','-d','auto_prepend_file=','-d','auto_append_file=',str(path)],timeout=20)
                evidence=json.loads(output)
            else:evidence=_request(str(self.host.path(target['socket'])),'/run/'+directory.name+'/probe.php')
            values=evidence.get('values',{})
            if (evidence.get('sapi')!=('cli' if scope=='cli' else 'fpm-fcgi')
                    or values.get('gate') is not enabled or values.get('sql_load_extension') is not enabled
                    or values.get('fresh_connection_unloaded') is not True
                    or (enabled and str(values.get('probe_value'))!=str(artifact['probe_value']))
                    or (not enabled and values.get('authorized_rejection') is not True)):
                raise RootActionError('Independent PHP SQL extension verification failed.','FEATHER_NOT_READY')
            return {'verified':True,'sql_load_extension':enabled,'fresh_connection_unloaded':True}
        finally:
            if path.exists():path.unlink()
            directory.rmdir()

    def _backup(self,identifier,aliases,active):
        directory=f'{BACKUPS}/{identifier}'
        self.core._secure_dir(directory)
        self.core._private_json(directory+'/feather.json',{'aliases':aliases,'active':active})
        return _sha(self.host.path(directory+'/feather.json').read_bytes())

    def _backup_read(self,journal):
        value=self._read_json(f'{BACKUPS}/{journal["transaction_id"]}/feather.json')
        if not value:raise RootActionError('Feather alias backup is unavailable.','RECOVERY_REQUIRED')
        if _sha(self.host.path(f'{BACKUPS}/{journal["transaction_id"]}/feather.json').read_bytes())!=journal.get('backup_sha256'):
            raise RootActionError('Feather alias backup integrity failed.','RECOVERY_REQUIRED')
        return value

    def _guard_aliases(self,journal):
        backup=self._backup_read(journal)
        active=self._active()
        current={}
        for scope in journal['scopes']:
            item=self._alias(scope,active,True)
            acceptable=[self._signature(backup['aliases'][scope])]
            if scope in journal.get('after_aliases',{}):acceptable.append(journal['after_aliases'][scope])
            if self._signature(item) not in acceptable:
                raise RootActionError('An external PHP alias edit prevents recovery.','RECOVERY_REQUIRED')
            current[scope]=item
        return backup,current

    def _restore_boot(self,target,service):
        current=self._service(target)
        if current['enabled']!=service['enabled']:
            self._checked(['systemctl','enable' if service['enabled'] else 'disable',target['unit']])

    def _rollback(self,journal,artifact=None):
        backup,_=self._guard_aliases(journal)
        for scope in journal['scopes']:
            current=self._alias(scope,self._active(),True)
            if self._signature(current)!=self._signature(backup['aliases'][scope]):
                self._replace_alias(scope,backup['aliases'][scope],current)
        self._write_active(backup['active'])
        if 'fpm' in journal['scopes'] and journal.get('activation_started'):
            self._fpm_reload(journal['target'],journal['service_before'])
            self._restore_boot(journal['target'],journal['service_before'])
            if not journal['service_before']['active']:self._checked(['systemctl','stop',journal['target']['unit']])
        if artifact and journal.get('activation_started'):
            for scope in journal['scopes']:
                if scope=='fpm' and not journal['service_before']['active']:continue
                prior=(backup['active'] or {}).get('scopes',{}).get(scope)
                self._probe(scope,prior['artifact'] if prior else artifact,prior is not None,journal['target'])

    def apply(self,plan):
        self._gate()
        if not isinstance(plan,dict) or plan.get('fingerprint')!=_fingerprint(plan):
            raise RootActionError('Feather preview was modified.','STALE_PLAN')
        identifier=None;artifact=None
        with self.core._lock():
            self.core._check_pending()
            trusted=self.plan(plan.get('cli'),plan.get('fpm'))
            if trusted['fingerprint']!=plan['fingerprint']:raise RootActionError('Feather preview is stale.','STALE_PLAN')
            active=self._active();scopes=[scope for scope in SCOPES if plan[scope]]
            aliases={scope:self._alias(scope,active) for scope in scopes}
            self.core._preflight_state();self.core._secure_dir(STATE)
            identifier=self.core._id();backup_sha=self._backup(identifier,aliases,active)
            journal={'schema':1,'module':'php-feather','kind':'php-feather','transaction_id':identifier,'status':'FEATHER_BUILDING',
                'scopes':scopes,'before':trusted['before'],'target':trusted['before']['target'],'service_before':trusted['before']['service'],
                'after_aliases':{},'activation_started':False,'backup_sha256':backup_sha}
            self.core._journal(journal)
        try:
            built=self.builder.build(trusted['build_plan'],transaction_id=identifier)
            artifact=self._validate_artifact(built)
            with self.core._lock():
                self.core._check_pending(ignore_id=identifier)
                saved=next(item for item in self.core._journals() if item['transaction_id']==identifier)
                if saved['status']!='FEATHER_BUILDING':raise RootActionError('Feather build was cancelled before activation.','FEATHER_BUILD_FAILED')
                if self._profile()!=trusted['before']:raise RootActionError('PHP changed during the build; activation was refused.','STALE_PLAN')
                current=copy.deepcopy(active) if active else {'schema':1,'scopes':{},'stock_aliases':{},'stock_service':None}
                current['transaction_id']=identifier
                for scope in scopes:
                    current['stock_aliases'].setdefault(scope,aliases[scope])
                    current['scopes'][scope]={'artifact':artifact,'build_fingerprint':trusted['build_plan']['fingerprint'],
                                             'previous_alias':aliases[scope]}
                    content=self._content(artifact)
                    journal['after_aliases'][scope]={'kind':'regular','target':None,'sha256':_sha(content)}
                if 'fpm' in scopes and current['stock_service'] is None:current['stock_service']=journal['service_before']
                journal.update(status='FEATHER_ACTIVATING',activation_started=True)
                self.core._journal(journal)
                # Manifest identifies the exact managed bytes even if the
                # process dies between alias rename and the next journal write.
                self._write_active(current)
                for scope in scopes:
                    self._replace_alias(scope,{'kind':'regular','target':None,'content':base64.b64encode(self._content(artifact)).decode(),
                        'metadata':{'mode':0o644,'uid':0,'gid':0,'xattrs':{}}},aliases[scope])
                if 'fpm' in scopes:self._fpm_reload(journal['target'],journal['service_before'])
                verification={scope:self._probe(scope,artifact,True,journal['target']) for scope in scopes}
                if 'fpm' in scopes and not journal['service_before']['active']:self._checked(['systemctl','stop',journal['target']['unit']])
                for scope in scopes:current['scopes'][scope].pop('previous_alias',None)
                self._write_active(current)
                journal['status']='FEATHER_APPLIED';self.core._journal(journal)
                return self.core._log_result({'status':'FEATHER_APPLIED','module':'php-feather','transaction_id':identifier,
                    'message':'Selected PHP SAPIs use the verified custom SQL-loading module; official module preserved.','verification':verification})
        except Exception as failure:
            with self.core._lock():
                current=next(item for item in self.core._journals() if item['transaction_id']==identifier)
                if current['status']=='FEATHER_ROLLED_BACK':
                    raise RootActionError('The Feather operation was recovered before activation.','FEATHER_BUILD_FAILED') from None
                try:self._rollback(journal,artifact)
                except Exception:
                    journal['status']='FEATHER_RECOVERY_REQUIRED';self.core._journal(journal)
                    raise RootActionError('Feather recovery requires inspection; external edits were preserved.','RECOVERY_REQUIRED') from None
                journal['status']='FEATHER_ROLLED_BACK' if journal['activation_started'] else 'FEATHER_FAILED'
                self.core._journal(journal)
                if journal['activation_started']:
                    return self.core._log_result({'status':'FEATHER_ROLLED_BACK','module':'php-feather','transaction_id':identifier,
                        'message':'Custom activation failed; previous PHP aliases, service and manifest were restored.'})
                if (isinstance(failure,RootActionError) and isinstance(failure.code,str)
                        and re.fullmatch(r'[A-Z0-9_]{1,64}',failure.code)):
                    raise RootActionError('The custom PDO SQLite build failed ('+failure.code+'). Inspect the private diagnostics directory: '+
                        DIAGNOSTICS+'. Official activation was preserved.',failure.code) from None
                raise RootActionError('The custom PDO SQLite build failed; official activation was preserved.','FEATHER_BUILD_FAILED') from None

    def restore_plan(self):
        self._gate()
        pending=self._pending()
        profile=self._profile(strict_artifacts=False,transition_scopes=pending[-1]['scopes'] if pending else ())
        active=self._active()
        service_target=(active or {}).get('stock_service')
        if pending:
            selected=pending[-1];scopes=selected['scopes'];mode='pending';identifier=selected['transaction_id']
            backup,_=self._guard_aliases(selected)
            # Cancelling a pending build is followed by Restore stock for all
            # previously managed SAPIs, including scopes not in that build.
            prior_scopes=(backup['active'] or {}).get('scopes',{})
            scopes=[scope for scope in SCOPES if scope in selected['scopes'] or scope in prior_scopes]
            service_target=(backup['active'] or {}).get('stock_service') or service_target
            if selected.get('operation')=='stock-restore':
                mode='resume-stock';service_target=(backup['active'] or {}).get('stock_service')
        else:
            if not active or not active['scopes']:raise RootActionError('No managed custom PHP activation exists.','NO_BACKUP')
            scopes=list(active['scopes']);mode='stock';identifier=active['transaction_id']
        self.core._check_pending(ignore_id=identifier)
        result={'schema':1,'module':'php-feather','transaction_id':identifier,'cli':'cli' in scopes,'fpm':'fpm' in scopes,
            'mode':mode,'before':profile,'aliases':{scope:profile['aliases'][scope] for scope in scopes},
            'service_target':service_target,
            'notes':['Restore the first official native aliases for managed SAPIs; other SAPIs are unchanged.',
                     'The official system .so and installed build dependencies are preserved.']}
        result['fingerprint']=_fingerprint(result);return result

    def _complete_stock_restore(self,journal):
        backup,current=self._guard_aliases(journal)
        active=backup['active']
        if not active:
            raise RootActionError('Stock restore metadata is unavailable.','RECOVERY_REQUIRED')
        for scope in journal['scopes']:
            target=active['stock_aliases'][scope]
            if self._signature(current[scope])!=self._signature(target):
                self._replace_alias(scope,target,current[scope])
        self._write_active(None)
        service=active['stock_service'] or journal['service_before']
        if 'fpm' in journal['scopes']:self._fpm_reload(journal['target'],service)
        verification={scope:self._probe(scope,active['scopes'][scope]['artifact'],False,journal['target']) for scope in journal['scopes']}
        if 'fpm' in journal['scopes']:
            self._restore_boot(journal['target'],service)
            if not service['active']:self._checked(['systemctl','stop',journal['target']['unit']])
        return verification

    def restore(self,plan):
        self._gate()
        if not isinstance(plan,dict) or plan.get('fingerprint')!=_fingerprint(plan):
            raise RootActionError('Feather restore preview was modified.','STALE_PLAN')
        with self.core._lock():
            trusted=self.restore_plan()
            if trusted['fingerprint']!=plan['fingerprint']:raise RootActionError('Feather restore preview is stale.','STALE_PLAN')
            selected_id=trusted['transaction_id']
            selected=next(item for item in self.core._journals() if item['transaction_id']==selected_id)
            if trusted['mode']=='pending':
                # Closing a build's durable pending record also makes a late
                # build result ineligible for activation in the original caller.
                self._rollback(selected)
                selected['status']='FEATHER_ROLLED_BACK';self.core._journal(selected)
                active=self._active()
                if not active or not active['scopes']:
                    return self.core._log_result({'status':'FEATHER_RESTORED','module':'php-feather','transaction_id':selected_id,
                        'message':'Pending custom build cancelled; official PHP aliases were preserved.'})
                profile=self._profile(strict_artifacts=False)
            else:profile=trusted['before']
            if trusted['mode']=='resume-stock':
                journal=selected
            else:
                active=self._active();scopes=list(active['scopes'])
                identifier=self.core._id()
                backup_sha=self._backup(identifier,{scope:self._alias(scope,active) for scope in scopes},active)
                journal={'schema':1,'module':'php-feather','kind':'php-feather','operation':'stock-restore',
                    'transaction_id':identifier,'status':'FEATHER_RESTORING','scopes':scopes,'before':profile,
                    'target':profile['target'],'service_before':profile['service'],'activation_started':True,
                    'backup_sha256':backup_sha,'after_aliases':{scope:self._signature(active['stock_aliases'][scope]) for scope in scopes}}
                self.core._journal(journal)
            try:
                verification=self._complete_stock_restore(journal)
            except Exception:
                journal['status']='FEATHER_RESTORE_ROLLING_BACK';self.core._journal(journal)
                try:
                    backup=self._backup_read(journal)
                    self._rollback(journal,next(iter(backup['active']['scopes'].values()))['artifact'])
                except Exception:
                    journal['status']='FEATHER_RECOVERY_REQUIRED';self.core._journal(journal)
                    raise RootActionError('Stock restore and recovery require inspection; the pending journal is retained.','RECOVERY_REQUIRED') from None
                journal['status']='FEATHER_ROLLED_BACK';self.core._journal(journal)
                return self.core._log_result({'status':'FEATHER_ROLLED_BACK','module':'php-feather','transaction_id':journal['transaction_id'],
                    'message':'Stock restore failed; the previous working custom PHP aliases, service and manifest were restored.'})
            journal['status']='FEATHER_ROLLED_BACK';self.core._journal(journal)
            return self.core._log_result({'status':'FEATHER_RESTORED','module':'php-feather','transaction_id':journal['transaction_id'],
                'message':'Managed PHP aliases restored to their official stock baseline; system module was never overwritten.','verification':verification})

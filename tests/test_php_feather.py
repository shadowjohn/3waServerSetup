"""Feather activation uses real aliases/files; only builder and native probes vary."""
import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from server_setting.root_actions import Host, RootActionError, _fingerprint
try:
    from server_setting.php_feather import FeatherSQLiteManager
except ImportError:
    FeatherSQLiteManager=None

STOCK=b'; configuration for php sqlite3 module\n; priority=20\nextension=pdo_sqlite.so\n'


class FeatherTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(FeatherSQLiteManager,'Feather activation backend is missing')
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        for name in ('etc/php/8.5/cli/conf.d','etc/php/8.5/fpm/conf.d','etc/php/8.5/mods-available',
                     'etc/php/8.5/fpm/pool.d','usr/lib/php/20250925','run/php'):
            (self.root/name).mkdir(parents=True,exist_ok=True)
        (self.root/'etc/os-release').write_text('ID=ubuntu\nVERSION_ID="26.04"\n')
        for scope in ('cli','fpm'):
            (self.root/f'etc/php/8.5/{scope}/php.ini').write_text('; clean native PHP\n')
            (self.root/f'etc/php/8.5/{scope}/conf.d/20-pdo_sqlite.ini').write_bytes(STOCK)
        (self.root/'etc/php/8.5/mods-available/pdo_sqlite.ini').write_bytes(STOCK)
        (self.root/'usr/lib/php/20250925/pdo_sqlite.so').write_bytes(b'official-module')
        (self.root/'etc/php/8.5/fpm/php-fpm.conf').write_text('include=/etc/php/8.5/fpm/pool.d/*.conf\n')
        (self.root/'etc/php/8.5/fpm/pool.d/www.conf').write_text('[www]\nuser=www-data\nlisten=/run/php/php8.5-fpm.sock\n')
        self.events=[];self.active=True;self.enabled=True;self.fail_probe=False;self.fail_build=False
        self.build_number=0;self.package_version='8.5.4-0ubuntu1.5';self.tamper_build=False
        self.fail_stock_probe=False;self.cancel_build=False;self.restore_preview=None
        self.builder_failure_code='FEATHER_BUILD_FAILED'
        outer=self

        class FakeHost(Host):
            def run(self,argv,timeout=30):
                outer.events.append(list(argv))
                output='';code=0
                if argv[:2]==['systemctl','show']:
                    output='LoadState=loaded\nActiveState='+('active' if outer.active else 'inactive')+'\nUnitFileState='+('enabled' if outer.enabled else 'disabled')+'\n'
                elif argv[0]=='systemctl':
                    if argv[1] in {'start','reload-or-restart','restart'}:outer.active=True
                    elif argv[1]=='stop':outer.active=False
                    elif argv[1]=='enable':outer.enabled=True
                    elif argv[1]=='disable':outer.enabled=False
                    else:raise AssertionError(argv)
                elif argv[0]=='php-fpm8.5':
                    outer.alias('fpm').read_bytes()
                elif argv[0]=='php8.5':
                    enabled=b'feather_sql_load_extension=On' in outer.alias('cli').read_bytes()
                    output=json.dumps(outer.evidence('cli',enabled))
                else:raise AssertionError(argv)
                return subprocess.CompletedProcess(argv,code,output,'')

        class FakeBuilder:
            def __init__(self,host):self.host=host
            def status(self):
                return {'supported':True,'native':{'php_version':'8.5.4','package_version':outer.package_version,
                        'abi':'20250925','arch':'amd64','nts':True},'detail':'Native matching build profile'}
            def plan(self):
                result={'schema':1,'module':'php-feather-build','native':self.status()['native'],
                        'dependencies':['php8.5-dev','build-essential'],'source_profile':{'version':outer.package_version}}
                result['fingerprint']=_fingerprint(result);return result
            def build(self,plan,transaction_id=None):
                journals=outer.manager.core._journals()
                assert any(item['transaction_id']==transaction_id and item['status']=='FEATHER_BUILDING' for item in journals)
                if outer.fail_build:raise RootActionError('A secret compiler diagnostic must not escape',outer.builder_failure_code)
                if outer.tamper_build:
                    self.host.path('/etc/php/8.5/cli/conf.d/20-pdo_sqlite.ini').write_bytes(b'; external edit\nextension=personal.so\n')
                if outer.cancel_build:
                    outer.restore_preview=outer.manager.restore_plan()
                    outer.manager.restore(outer.restore_preview)
                outer.build_number+=1
                directory=f'/opt/3waServerSetting/modules/php-feather/test-{outer.build_number}'
                self.host.path(directory).mkdir(parents=True)
                self.host.path(directory+'/pdo_sqlite.so').write_bytes(('custom-'+str(outer.build_number)).encode())
                self.host.path(directory+'/probe.so').write_bytes(b'probe')
                artifact={'module_path':directory+'/pdo_sqlite.so','sha256':hashlib.sha256(self.host.path(directory+'/pdo_sqlite.so').read_bytes()).hexdigest(),
                    'abi':'20250925','package_version':outer.package_version,'php_version':'8.5.4','arch':'amd64','nts':True,
                    'source_sha256':'source','patch_sha256':'patch','probe_so_path':directory+'/probe.so',
                    'probe_sha256':hashlib.sha256(b'probe').hexdigest(),'probe_function':'threewa_probe','probe_value':'feather-ok',
                    'buildmanifest':directory+'/manifest.json'}
                self.host.path(artifact['buildmanifest']).write_text(json.dumps(artifact))
                self.host.path(artifact['buildmanifest']).chmod(0o600)
                return artifact
            def validate_artifact(self,artifact,host=None):
                self_host=host or self.host
                if artifact['package_version']!=outer.package_version:raise RootActionError('Artifact ABI/version stale','STALE_PLAN')
                if hashlib.sha256(self_host.path(artifact['module_path']).read_bytes()).hexdigest()!=artifact['sha256']:
                    raise RootActionError('Artifact changed','STALE_PLAN')
                return artifact

        self.host=FakeHost(self.root)
        self.builder=FakeBuilder(self.host)
        self.manager=FeatherSQLiteManager(self.host,builder=self.builder)
        self.addCleanup(patch.stopall)
        patch('server_setting.root_actions.os.geteuid',return_value=0,create=True).start()
        @contextmanager
        def unlocked():yield
        patch.object(self.manager.core,'_lock',unlocked).start()
        patch.object(self.manager,'_fpm_target',return_value={'version':'8.5','unit':'php8.5-fpm.service','socket':'/run/php/php8.5-fpm.sock','binary':'php-fpm8.5','pool':'www'}).start()
        def request(socket_path,script_path):
            path=self.host.path(script_path)
            self.assertTrue(path.exists())
            self.assertIn('load_extension',path.read_text())
            self.assertNotIn('phpinfo',path.read_text())
            enabled=b'feather_sql_load_extension=On' in self.alias('fpm').read_bytes()
            return self.evidence('fpm-fcgi',enabled)
        patch('server_setting.root_fpm._request',side_effect=request).start()

    def evidence(self,sapi,enabled):
        if self.fail_stock_probe and not enabled and sapi=='fpm-fcgi':
            self.fail_stock_probe=False
            return {'sapi':sapi,'values':{'error':'stock verification failure'}}
        if self.fail_probe and enabled:
            self.fail_probe=False
            return {'sapi':sapi,'values':{'gate':True,'sql_load_extension':False,'probe_value':None,'fresh_connection_unloaded':True}}
        return {'sapi':sapi,'values':{'gate':enabled,'sql_load_extension':enabled,'authorized_rejection':not enabled,
                'probe_value':'feather-ok' if enabled else None,'fresh_connection_unloaded':True}}

    def alias(self,scope):
        # 原生 PHP 會跟隨官方 alias；fixture 僅檢查父目錄，保留 leaf 的 symlink 語意。
        return self.host.path('/etc/php/8.5/'+scope+'/conf.d')/'20-pdo_sqlite.ini'

    def test_status_and_plan_are_readonly_and_scopes_are_explicit(self):
        self.assertFalse(self.manager.status()['cli_enabled'])
        plan=self.manager.plan(cli=True,fpm=False)
        self.assertEqual((plan['cli'],plan['fpm']),(True,False))
        self.assertIn('build_plan',plan)
        self.assertFalse(self.host.path('/var').exists())
        self.assertEqual(self.alias('cli').read_bytes(),STOCK)
        self.assertEqual(self.build_number,0)
        with self.assertRaises((RootActionError,ValueError)):self.manager.plan(False,False)

    def test_cli_only_build_activate_preserves_fpm_and_system_module(self):
        result=self.manager.apply(self.manager.plan(cli=True,fpm=False))
        self.assertEqual(result['status'],'FEATHER_APPLIED')
        self.assertIn(b'/opt/3waServerSetting/modules/',self.alias('cli').read_bytes())
        self.assertIn(b'feather_sql_load_extension=On',self.alias('cli').read_bytes())
        self.assertEqual(self.alias('fpm').read_bytes(),STOCK)
        self.assertEqual(self.host.path('/usr/lib/php/20250925/pdo_sqlite.so').read_bytes(),b'official-module')
        self.assertFalse(any(row[0]=='systemctl' and row[1]!='show' for row in self.events))
        self.assertTrue(self.manager.status()['cli_enabled'])

    def test_stale_preview_and_custom_ini_refuse_before_build(self):
        plan=self.manager.plan()
        plan['cli']=False
        with self.assertRaises(RootActionError):self.manager.apply(plan)
        self.alias('cli').write_bytes(b'extension=private.so\n')
        with self.assertRaises(RootActionError):self.manager.plan()
        self.assertEqual(self.build_number,0)

    def test_probe_failure_rolls_back_both_aliases_service_and_manifest(self):
        self.fail_probe=True
        result=self.manager.apply(self.manager.plan())
        self.assertEqual(result['status'],'FEATHER_ROLLED_BACK')
        self.assertEqual(self.alias('cli').read_bytes(),STOCK)
        self.assertEqual(self.alias('fpm').read_bytes(),STOCK)
        self.assertTrue(self.active);self.assertTrue(self.enabled)
        self.assertFalse(self.manager.status()['cli_enabled'])
        self.assertEqual(list(self.host.path('/run').glob('3wa-feather-*')),[])

    def test_build_failure_has_no_activation_and_preserves_official_aliases(self):
        self.fail_build=True
        with self.assertRaises(RootActionError) as failure:self.manager.apply(self.manager.plan())
        self.assertNotIn('secret compiler diagnostic',str(failure.exception))
        self.assertEqual(self.alias('cli').read_bytes(),STOCK)
        self.assertEqual(self.alias('fpm').read_bytes(),STOCK)
        self.assertEqual(self.manager.core._journals()[-1]['status'],'FEATHER_FAILED')

    def test_external_alias_edit_during_build_is_never_overwritten(self):
        self.tamper_build=True
        with self.assertRaises(RootActionError):self.manager.apply(self.manager.plan())
        self.assertEqual(self.alias('cli').read_bytes(),b'; external edit\nextension=personal.so\n')
        self.assertEqual(self.alias('fpm').read_bytes(),STOCK)

    def test_two_custom_builds_then_restore_returns_first_official_baseline(self):
        self.manager.apply(self.manager.plan())
        first=self.alias('cli').read_bytes()
        self.manager.apply(self.manager.plan())
        self.assertNotEqual(first,self.alias('cli').read_bytes())
        preview=self.manager.restore_plan()
        self.assertTrue(preview['cli']);self.assertTrue(preview['fpm'])
        self.manager.restore(preview)
        self.assertEqual(self.alias('cli').read_bytes(),STOCK)
        self.assertEqual(self.alias('fpm').read_bytes(),STOCK)
        self.assertFalse(self.manager.status()['cli_enabled'])

    def test_restore_blocks_external_edits_and_version_changed_activation(self):
        self.manager.apply(self.manager.plan(cli=True,fpm=False))
        preview=self.manager.restore_plan()
        self.alias('cli').write_bytes(self.alias('cli').read_bytes()+b'; external\n')
        with self.assertRaises(RootActionError):self.manager.restore(preview)
        self.assertIn(b'external',self.alias('cli').read_bytes())

    def test_native_stock_symlink_is_replaced_and_restored_without_following_target(self):
        self.alias('cli').unlink()
        try:self.alias('cli').symlink_to('../../mods-available/pdo_sqlite.ini')
        except OSError:self.skipTest('Native symlink privilege unavailable')
        original=self.host.path('/etc/php/8.5/mods-available/pdo_sqlite.ini').read_bytes()
        self.manager.apply(self.manager.plan(cli=True,fpm=False))
        self.assertFalse(self.alias('cli').is_symlink())
        result=self.manager.restore(self.manager.restore_plan())
        self.assertEqual(result['status'],'FEATHER_RESTORED')
        self.assertTrue(self.alias('cli').is_symlink())
        self.assertEqual(self.alias('cli').readlink().as_posix(),'../../mods-available/pdo_sqlite.ini')
        self.assertEqual(self.host.path('/etc/php/8.5/mods-available/pdo_sqlite.ini').read_bytes(),original)

    def test_restore_verification_failure_compensates_previous_custom_activation(self):
        self.manager.apply(self.manager.plan())
        before={scope:self.alias(scope).read_bytes() for scope in ('cli','fpm')}
        manifest=self.host.path('/var/lib/3wa-server-setup/php-feather/active.json').read_bytes()
        self.fail_stock_probe=True
        result=self.manager.restore(self.manager.restore_plan())
        self.assertEqual(result['status'],'FEATHER_ROLLED_BACK')
        self.assertEqual({scope:self.alias(scope).read_bytes() for scope in before},before)
        self.assertEqual(self.host.path('/var/lib/3wa-server-setup/php-feather/active.json').read_bytes(),manifest)
        self.assertTrue(self.manager.status()['cli_enabled'])
        self.assertTrue(self.manager.status()['fpm_enabled'])
        self.manager.core._check_pending()

    def test_build_completion_after_cancel_does_not_activate(self):
        self.cancel_build=True
        with self.assertRaises(RootActionError):self.manager.apply(self.manager.plan())
        self.assertEqual(self.alias('cli').read_bytes(),STOCK)
        self.assertEqual(self.alias('fpm').read_bytes(),STOCK)
        self.manager.core._check_pending()

    def test_unselected_existing_custom_fpm_is_preserved_on_cli_activation(self):
        self.manager.apply(self.manager.plan(cli=False,fpm=True))
        before=self.alias('fpm').read_bytes()
        self.events.clear()
        self.manager.apply(self.manager.plan(cli=True,fpm=False))
        self.assertEqual(self.alias('fpm').read_bytes(),before)
        self.assertFalse(any(row[0]=='systemctl' and row[1]!='show' for row in self.events))

    def test_real_builder_and_engine_status_plan_share_the_live_contract(self):
        from server_setting.php_feather_build import FeatherBuilder
        import server_setting.php_feather_build as build_module
        worker_bytes=Path(build_module.__file__).resolve().read_bytes()
        worker=self.root/'opt/fixture-code/server_setting/php_feather_build.py'
        worker.parent.mkdir(parents=True)
        worker.write_bytes(worker_bytes);worker.chmod(0o644)
        for parent in [worker.parent,*worker.parent.parents]:
            parent.chmod(0o755)
            if parent==self.root:break
        native={**self.builder.status()['native'],'debug':False,'source_package':'php8.5',
                'sqlite_package_version':'3.46.1-9ubuntu0.3','extension_dir':'/usr/lib/php/20250925',
                'original_module':'/usr/lib/php/20250925/pdo_sqlite.so',
                'original_sha256':hashlib.sha256(b'official-module').hexdigest(),
                'pdo_module':'/usr/lib/php/20250925/pdo.so'}
        self.host.inspect=lambda:dict(native)
        self.host.path('/etc/apt/sources.list.d').mkdir(parents=True)
        self.host.path('/usr/share/keyrings').mkdir(parents=True)
        self.host.path('/usr/share/keyrings/ubuntu-archive-keyring.gpg').write_bytes(b'fixture archive keyring')
        self.host.path('/etc/apt/sources.list.d/ubuntu.sources').write_text(
            'Types: deb\nURIs: http://tw.archive.ubuntu.com/ubuntu/\nSuites: resolute resolute-updates\n'
            'Components: main universe\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n')
        class PackagesBoundary:
            def plan(self,names):
                result={'schema':1,'module':'packages','names':sorted(names),
                    'observed':[{'name':name,'installed':True,
                      'current':native['package_version'] if name=='php8.5-dev' else native['sqlite_package_version'] if name=='libsqlite3-dev' else '1',
                      'candidate':'1'} for name in sorted(names)],'changes':[],
                    'command':['apt-get','--no-upgrade','--no-remove','--simulate','install','--',*sorted(names)]}
                result['fingerprint']=_fingerprint(result);return result
        real=FeatherBuilder(self.host)
        with patch.object(real,'_packages',return_value=PackagesBoundary()), \
             patch.object(build_module,'WORKER_FILE',worker), \
             patch.object(build_module,'_WORKER_SOURCE_SHA256',hashlib.sha256(worker_bytes).hexdigest()):
            engine=FeatherSQLiteManager(self.host,builder=real)
            with patch.object(engine,'_fpm_target',return_value=self.manager._fpm_target()):
                self.assertEqual(real.status()['native']['package_version'],self.package_version)
                self.assertTrue(engine.status()['supported'])
                plan=engine.plan(cli=True,fpm=False)
                self.assertEqual(plan['build_plan']['native']['abi'],plan['before']['native']['abi'])
                self.assertIn('deb-src',plan['build_plan']['source_profile']['deb_src'])
                self.assertIn('--no-install-recommends',plan['build_plan']['dependencies']['command'])
                self.assertFalse(self.host.path('/var').exists())

    def test_pending_cli_restore_reviews_existing_custom_fpm_and_restores_both_stock(self):
        self.manager.apply(self.manager.plan(cli=False,fpm=True))
        self.assertTrue(self.manager.status()['fpm_enabled'])
        self.cancel_build=True
        with self.assertRaises(RootActionError):
            self.manager.apply(self.manager.plan(cli=True,fpm=False))
        preview=self.restore_preview
        self.assertTrue(preview['cli'])
        self.assertTrue(preview['fpm'])
        self.assertEqual(set(preview['aliases']),{'cli','fpm'})
        self.assertTrue(preview['before']['aliases']['fpm']['managed'])
        self.assertEqual(self.alias('cli').read_bytes(),STOCK)
        self.assertEqual(self.alias('fpm').read_bytes(),STOCK)
        self.assertFalse(self.manager.status()['cli_enabled'])
        self.assertFalse(self.manager.status()['fpm_enabled'])
        self.manager.core._check_pending()

    def test_build_failure_preserves_only_safe_code_and_fixed_diagnostics_directory(self):
        self.fail_build=True
        for code,expected in (('FEATHER_SOURCE_LAYOUT','FEATHER_SOURCE_LAYOUT'),
                              ('FEATHER_SOURCE_LAYOUT\nsecret-details','FEATHER_BUILD_FAILED')):
            self.builder_failure_code=code
            with self.subTest(code=code),self.assertRaises(RootActionError) as failure:
                self.manager.apply(self.manager.plan())
            self.assertEqual(failure.exception.code,expected)
            if expected=='FEATHER_SOURCE_LAYOUT':
                self.assertIn(expected,str(failure.exception))
                self.assertIn('/var/lib/3wa-server-setup/feather-build/diagnostics',str(failure.exception))
            self.assertNotIn('secret compiler diagnostic',str(failure.exception))
            self.assertNotIn('secret-details',str(failure.exception))
            self.assertEqual(self.manager.core._journals()[-1]['status'],'FEATHER_FAILED')


if __name__=='__main__':unittest.main()

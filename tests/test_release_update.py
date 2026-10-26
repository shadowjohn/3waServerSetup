"""Signed, filesystem-backed release transactions without touching host services."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

try:
    from server_setting.release_update import ReleaseManager, ReleaseError, ensure_current_release, _version, validate_manifest
except ImportError:
    ReleaseManager = ReleaseError = ensure_current_release = None


class ReleaseUpdateTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(ReleaseManager, 'Release transaction engine is missing')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base/'install'
        self.key = self.base/'signer'
        self.ssh = shutil.which('ssh-keygen')
        self.assertTrue(self.ssh, 'OpenSSH signature fixtures require ssh-keygen')
        subprocess.run([self.ssh,'-q','-t','ed25519','-N','','-f',str(self.key)],check=True,capture_output=True)
        self.signers = self.base/'allowed_signers'
        self.signers.write_text('3waServerSetup-release '+Path(str(self.key)+'.pub').read_text().strip()+'\n',encoding='utf-8')
        self.fail_smoke = False
        self.calls = []
        def runner(argv, timeout=30, input=None, cwd=None):
            self.calls.append(list(argv))
            if Path(argv[0]).name.startswith('ssh-keygen'):
                return subprocess.run(argv,input=input,capture_output=True,timeout=timeout,cwd=cwd)
            if '-m' in argv and 'venv' in argv:
                target = Path(argv[-1]); (target/'bin').mkdir(parents=True)
                (target/'bin/python').write_text('#!/bin/sh\nexit 0\n',encoding='utf-8')
                (target/'bin/python').chmod(0o755)
            code = int(self.fail_smoke and '--smoke' in argv)
            return subprocess.CompletedProcess(argv,code,b'',b'')
        self.manager = ReleaseManager(self.root,runner=runner)
        self.addCleanup(patch.stopall)
        patch.object(self.manager,'_gate',return_value=None).start()
        self.first = self.bundle('2026.10.25.001', 'a'*40)

    def bundle(self, version, commit, mutate=None, *, identity_changes=None):
        folder = self.base/('bundle-'+version); folder.mkdir()
        identity = {'schema':1,'product':'3waServerSetting','repository':'shadowjohn/3waServerSetup',
            'version':version,'tag':'v'+version,'commit':commit,'data_schema':1,
            'python_requires':'>=3.10','validated_platforms':['ubuntu26.04']}
        identity.update(identity_changes or {})
        payload = {'run.sh':b'#!/bin/sh\nexit 0\n','server_setting/__init__.py':b"VERSION='0.1.0-dev'\n",
            'server_setting/__main__.py':b'pass\n','server_setting/root_actions.py':b'pass\n',
            'server_setting/release_update.py':b'pass\n','firewall_plan.py':b'pass\n',
            'config/firewall.example.json':b'{}\n','requirements-tui.lock':b'fixture==1 --hash=sha256:'+b'0'*64+b'\n',
            'wheelhouse/fixture-1-py3-none-any.whl':b'fixture wheel',
            'release/allowed_signers':self.signers.read_bytes(),
            'RELEASE.json':json.dumps(identity,sort_keys=True).encode()+b'\n'}
        if mutate:
            mutate(payload)
        archive = folder/('3waServerSetting-v'+version+'.tar.gz')
        with tarfile.open(archive,'w:gz') as stream:
            for name, data in payload.items():
                info=tarfile.TarInfo('3waServerSetting/'+name);info.size=len(data);info.mode=0o755 if name=='run.sh' else 0o644
                stream.addfile(info,io.BytesIO(data))
        manifest = dict(identity,archive={'name':archive.name,'sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'size':archive.stat().st_size},
            installer={'name':'install.sh','sha256':'1'*64,'size':1},
            files={name:{'sha256':hashlib.sha256(data).hexdigest(),'size':len(data),'mode':0o755 if name=='run.sh' else 0o644} for name,data in payload.items()})
        path=folder/'release-manifest.json';path.write_text(json.dumps(manifest,sort_keys=True)+'\n',encoding='utf-8')
        subprocess.run([self.ssh,'-Y','sign','-f',str(self.key),'-n','3wa-release@3wa.tw',str(path)],check=True,capture_output=True)
        return {'archive':archive,'manifest':path,'signature':Path(str(path)+'.sig'),'signers':self.signers,'identity':identity}

    def install(self, bundle):
        return self.manager.install_local(**{key:bundle[key] for key in ('archive','manifest','signature','signers')})

    def test_status_is_local_and_unmanaged_is_explicit(self):
        value=self.manager.status()
        self.assertFalse(value['managed'])
        self.assertFalse(self.root.exists())
        self.assertFalse(self.calls)

    def test_calendar_versions_and_rc_ordering_are_unambiguous(self):
        self.assertLess(_version('v2026.10.25.001-rc.9'),_version('v2026.10.25.001'))
        self.assertLess(_version('v2026.10.25.001'),_version('v2026.10.25.002'))
        self.assertLess(_version('v2026.10.25.999'),_version('v2026.10.26.001'))
        self.assertTrue(_version('v2028.02.29.001'))
        for value in ('v2026.02.29.001','v2026.13.01.001','v2026.10.25.000','v2026.10.25.01','v2026.10.25.001-rc.0'):
            with self.subTest(value=value),self.assertRaises(ReleaseError):_version(value)

    def test_install_prefix_cannot_alias_production_through_parent_segments(self):
        for value in ('/opt/../opt/3waServerSetting','//opt/3waServerSetting'):
            with self.subTest(value=value),self.assertRaises(ReleaseError) as error:ReleaseManager(value)
            self.assertEqual(error.exception.code,'INVALID_ROOT')

    def test_unknown_schema_or_missing_wheel_is_not_executed(self):
        value=json.loads(self.first['manifest'].read_text())
        value['schema']=2
        with self.assertRaises(ReleaseError):validate_manifest(json.dumps(value).encode())
        value['schema']=1
        value['files']={name:entry for name,entry in value['files'].items() if not name.startswith('wheelhouse/')}
        with self.assertRaises(ReleaseError):validate_manifest(json.dumps(value).encode())
        self.assertFalse(self.calls)

    def test_bridge_accepts_known_python_floors_and_platforms(self):
        original=json.loads(self.first['manifest'].read_text())
        for requirement in ('>=3.12','>=3.10'):
            for platforms in (['ubuntu22.04'],['ubuntu24.04'],['ubuntu26.04'],
                              ['ubuntu22.04','ubuntu24.04','ubuntu26.04']):
                value=dict(original,python_requires=requirement,validated_platforms=platforms)
                with self.subTest(requirement=requirement,platforms=platforms):
                    self.assertEqual(validate_manifest(json.dumps(value).encode()),value)
        self.assertFalse(self.calls)

    def test_bridge_rejects_unknown_or_malformed_python_and_platform_metadata(self):
        original=json.loads(self.first['manifest'].read_text())
        for requirement in ('>=3.9','>=3.11','3.10','>=3.10,<4',None,True,[],{}):
            value=dict(original,python_requires=requirement)
            with self.subTest(requirement=requirement):
                with self.assertRaises(ReleaseError) as error:validate_manifest(json.dumps(value).encode())
                self.assertEqual(error.exception.code,'INCOMPATIBLE_RELEASE')
        for platforms in ('ubuntu22.04',None,['ubuntu20.04'],['ubuntu22.04','ubuntu22.04'],
                          ['ubuntu22.04','ubuntu99.04'],[{}],[['ubuntu22.04']],[22]):
            value=dict(original,validated_platforms=platforms)
            with self.subTest(platforms=platforms):
                with self.assertRaises(ReleaseError) as error:validate_manifest(json.dumps(value).encode())
                self.assertEqual(error.exception.code,'BAD_MANIFEST')
        self.assertFalse(self.calls)

    def test_native_python310_and_supported_os_gates(self):
        for release,version,expected in (('22.04',(3,10),None),('24.04',(3,12),None),
                                         ('26.04',(3,14),None),('20.04',(3,10),'UNSUPPORTED_OS'),
                                         ('22.04',(3,9),'UNSUPPORTED_PYTHON')):
            with self.subTest(release=release,version=version), \
                    patch('server_setting.release_update.platform.system',return_value='Linux'), \
                    patch('server_setting.release_update.os.geteuid',return_value=0,create=True), \
                    patch('server_setting.release_update.sys.version_info',version), \
                    patch.object(Path,'read_text',return_value='ID=ubuntu\nVERSION_ID="'+release+'"\n'):
                if expected:
                    with self.assertRaises(ReleaseError) as error:ReleaseManager._gate(self.manager)
                    self.assertEqual(error.exception.code,expected)
                else:
                    ReleaseManager._gate(self.manager)

    @unittest.skipUnless(os.name=='posix','Native atomic symlink transactions require POSIX')
    def test_bridge_installs_and_rolls_back_python310_release(self):
        first=self.bundle('2026.10.25.002','b'*40,identity_changes={'python_requires':'>=3.12'})
        second=self.bundle('2026.10.25.003','c'*40,identity_changes={
            'python_requires':'>=3.10','validated_platforms':['ubuntu22.04','ubuntu24.04','ubuntu26.04']})
        with patch('server_setting.release_update.sys.version_info',(3,12)):
            self.install(first)
            original=os.readlink(self.root/'current')
            self.assertEqual(self.install(second)['status'],'UPDATED')
            self.assertEqual(self.manager.status()['installed']['validated_platforms'],
                             ['ubuntu22.04','ubuntu24.04','ubuntu26.04'])
            self.assertEqual(self.manager.rollback(self.manager.rollback_plan())['status'],'UPDATE_ROLLED_BACK')
            self.assertEqual(os.readlink(self.root/'current'),original)

    def test_python310_refuses_signed_python312_install_before_writes(self):
        bundle=self.bundle('2026.10.25.002','b'*40,identity_changes={'python_requires':'>=3.12'})
        with patch('server_setting.release_update.sys.version_info',(3,10)):
            with self.assertRaises(ReleaseError) as error:self.install(bundle)
        self.assertEqual(error.exception.code,'UNSUPPORTED_PYTHON')
        self.assertFalse(self.root.exists())
        self.assertFalse(any('-m' in argv and 'venv' in argv for argv in self.calls))

    def test_signed_update_preview_checks_actual_python_not_validation_platforms(self):
        bundle=self.bundle('2026.10.25.002','b'*40,identity_changes={'python_requires':'>=3.12'})
        tag=bundle['identity']['tag'];prefix='https://github.com/shadowjohn/3waServerSetup/releases/download/'+tag+'/'
        manifest=json.loads(bundle['manifest'].read_text())
        api={'tag_name':tag,'draft':False,'prerelease':False,'body':'Release notes','assets':[
            {'name':name,'browser_download_url':prefix+name} for name in
            ('release-manifest.json','release-manifest.json.sig',manifest['archive']['name'])]}
        responses={'https://api.github.com/repos/shadowjohn/3waServerSetup/releases/latest':json.dumps(api).encode(),
            'https://api.github.com/repos/shadowjohn/3waServerSetup/commits/'+tag:json.dumps({'sha':'b'*40}).encode(),
            prefix+'release-manifest.json':bundle['manifest'].read_bytes(),
            prefix+'release-manifest.json.sig':bundle['signature'].read_bytes()}
        self.manager.fetcher=lambda url,limit:responses[url]
        with patch.object(self.manager,'_signers',return_value=self.signers.read_bytes()), \
                patch.object(self.manager,'status',return_value={'supported':True,'installed':None}), \
                patch('server_setting.release_update.sys.version_info',(3,10)):
            with self.assertRaises(ReleaseError) as error:self.manager.check()
            self.assertEqual(error.exception.code,'UNSUPPORTED_PYTHON')
        with patch.object(self.manager,'_signers',return_value=self.signers.read_bytes()), \
                patch.object(self.manager,'status',return_value={'supported':True,'installed':None}), \
                patch('server_setting.release_update.sys.version_info',(3,12)):
            self.assertEqual(self.manager.check()['status'],'UPDATE_AVAILABLE')
        self.assertFalse(self.root.exists())

    def test_python310_refuses_python312_rollback_before_lock_or_mutation(self):
        old=json.loads(self.first['manifest'].read_text());old['python_requires']='>=3.12'
        new=dict(old,python_requires='>=3.10')
        state={'schema':1,'current':'new','previous':'old','launcher_sha256':'1'*64}
        with patch.object(self.manager,'_state',return_value=state), \
                patch.object(self.manager,'_journal',return_value=None), \
                patch.object(self.manager,'_current',return_value='new'), \
                patch.object(self.manager,'_installed_manifest',side_effect=lambda name,verify=False:old if name=='old' else new):
            with patch('server_setting.release_update.sys.version_info',(3,12)):
                plan=self.manager.rollback_plan()
            with patch('server_setting.release_update.sys.version_info',(3,10)):
                for action in (self.manager.rollback_plan,lambda:self.manager.rollback(plan)):
                    with self.subTest(action=action),patch.object(self.manager,'_lock') as lock:
                        with self.assertRaises(ReleaseError) as error:action()
                        self.assertEqual(error.exception.code,'UNSUPPORTED_PYTHON')
                        lock.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_wrong_signature_namespace_is_rejected(self):
        alternate=self.first['manifest'].with_name('wrong-namespace.json')
        alternate.write_bytes(self.first['manifest'].read_bytes())
        subprocess.run([self.ssh,'-Y','sign','-f',str(self.key),'-n','other-domain',str(alternate)],check=True,capture_output=True)
        self.first['signature']=Path(str(alternate)+'.sig')
        with self.assertRaises(ReleaseError) as error:self.install(self.first)
        self.assertEqual(error.exception.code,'BAD_SIGNATURE')
        self.assertFalse(self.root.exists())

    def test_running_legacy_console_blocks_adoption(self):
        entry=self.base/'proc/999999999';entry.mkdir(parents=True)
        (entry/'cmdline').write_bytes(b'python3\x00-m\x00server_setting\x00')
        original_dir=Path.is_dir;original_iter=Path.iterdir;original_resolve=Path.resolve
        with patch.object(Path,'is_dir',lambda path:True if path==Path('/proc') else original_dir(path)), \
             patch.object(Path,'iterdir',lambda path:iter([entry]) if path==Path('/proc') else original_iter(path)), \
             patch.object(Path,'resolve',lambda path,*a,**k:self.root if path==entry/'cwd' else original_resolve(path,*a,**k)):
            with self.assertRaises(ReleaseError) as error:self.manager._check_legacy_console(None)
        self.assertEqual(error.exception.code,'BUSY')

    def test_unknown_legacy_package_data_is_not_silently_omitted_from_rollback(self):
        self.legacy_fixture()
        unknown=self.root/'server_setting/private-settings.dat';unknown.write_bytes(b'private state')
        with self.assertRaises(ReleaseError) as error:self.manager._legacy('unused-candidate')
        self.assertEqual(error.exception.code,'UNRECOGNIZED_INSTALL')
        self.assertEqual(unknown.read_bytes(),b'private state')
        self.assertFalse((self.root/'server_setting').is_symlink())

    @unittest.skipUnless(os.name=='posix','Native atomic symlink transactions require POSIX')
    def test_signature_verified_install_upgrade_and_rollback_preserve_private_data(self):
        self.root.mkdir(); (self.root/'config').mkdir(); (self.root/'config/private.json').write_bytes(b'private')
        result=self.install(self.first)
        self.assertEqual(result['status'],'UPDATED')
        first=self.root.joinpath('current').resolve()
        second=self.bundle('2026.10.25.002','b'*40)
        self.install(second)
        self.assertNotEqual(first,self.root.joinpath('current').resolve())
        result=self.manager.rollback(self.manager.rollback_plan())
        self.assertEqual(result['status'],'UPDATE_ROLLED_BACK')
        self.assertEqual(first,self.root.joinpath('current').resolve())
        self.assertEqual((self.root/'config/private.json').read_bytes(),b'private')
        self.assertFalse(any('apt-get' in argv for argv in self.calls))

    def test_wrong_signature_and_archive_tamper_do_not_create_install(self):
        self.first['manifest'].write_bytes(self.first['manifest'].read_bytes()+b' ')
        with self.assertRaises(ReleaseError) as error:self.install(self.first)
        self.assertEqual(error.exception.code,'BAD_SIGNATURE')
        self.assertFalse((self.root/'current').exists())
        good=self.bundle('2026.10.25.002','b'*40);good['archive'].write_bytes(good['archive'].read_bytes()+b'x')
        with self.assertRaises(ReleaseError):self.install(good)
        self.assertFalse((self.root/'current').exists())

    def test_signed_traversal_and_private_payload_are_rejected(self):
        for index,name in enumerate(('../outside','config/startup/private.json','backups/secret','server_setting/link/escape.py')):
            bundle=self.bundle('2026.10.25.'+str(index+3).zfill(3),'c'*40,lambda files:files.update({name:b'x'}))
            with self.subTest(name=name),self.assertRaises(ReleaseError):self.install(bundle)
        self.assertFalse((self.base/'outside').exists())

    @unittest.skipUnless(os.name=='posix','Native atomic symlink transactions require POSIX')
    def test_failed_smoke_leaves_previous_current_and_data_intact(self):
        self.install(self.first); before=os.readlink(self.root/'current')
        self.fail_smoke=True
        with self.assertRaises(ReleaseError) as error:self.install(self.bundle('2026.10.25.002','b'*40))
        self.assertEqual(error.exception.code,'SMOKE_FAILED')
        self.assertEqual(os.readlink(self.root/'current'),before)

    @unittest.skipUnless(os.name=='posix','Native atomic symlink transactions require POSIX')
    def test_installed_key_is_pinned(self):
        self.install(self.first)
        second=self.bundle('2026.10.25.002','b'*40)
        alternate=self.base/'different';subprocess.run([self.ssh,'-q','-t','ed25519','-N','','-f',str(alternate)],check=True,capture_output=True)
        second['signers'].write_text('3waServerSetup-release '+Path(str(alternate)+'.pub').read_text().strip()+'\n',encoding='utf-8')
        with self.assertRaises(ReleaseError):self.install(second)
        self.assertEqual(self.manager.status()['installed']['version'],'2026.10.25.001')

    @unittest.skipUnless(os.name=='posix','Native atomic symlink transactions require POSIX')
    def test_stale_rollback_and_external_code_changes_refuse(self):
        self.install(self.first);self.install(self.bundle('2026.10.25.002','b'*40))
        plan=self.manager.rollback_plan()
        target=self.root/'releases'/plan['target']['directory']/'server_setting/__main__.py'
        target.write_bytes(b'changed\n')
        with self.assertRaises(ReleaseError):self.manager.rollback(plan)

    @unittest.skipUnless(os.name=='posix','Native atomic symlink transactions require POSIX')
    def test_interrupted_switch_recovers_original_without_running_it(self):
        self.install(self.first); old=os.readlink(self.root/'current')
        original=self.manager._write_state
        def interrupted(value):
            raise KeyboardInterrupt('power loss after current switch')
        with patch.object(self.manager,'_write_state',side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):self.install(self.bundle('2026.10.25.002','b'*40))
        self.assertTrue(self.manager.status()['pending'])
        count=len([argv for argv in self.calls if '--smoke' in argv])
        result=self.manager.rollback(self.manager.rollback_plan())
        self.assertEqual(result['status'],'UPDATE_ROLLED_BACK')
        self.assertEqual(os.readlink(self.root/'current'),old)
        self.assertEqual(count,len([argv for argv in self.calls if '--smoke' in argv]))
        self.assertIsNone(self.manager.status()['pending'])

    def test_pending_host_transaction_blocks_install(self):
        directory=self.root/'.update/transactions/fixture';directory.mkdir(parents=True)
        (directory/'journal.json').write_text(json.dumps({'transaction_id':'fixture','status':'SWAP_APPLYING'}),encoding='utf-8')
        with self.assertRaises(ReleaseError) as error:self.install(self.first)
        self.assertEqual(error.exception.code,'RECOVERY_REQUIRED')

    @unittest.skipUnless(os.name=='posix','Native atomic symlink transactions require POSIX')
    def test_foreign_alias_is_not_overwritten(self):
        self.root.mkdir();(self.root/'server_setting').symlink_to(self.base,target_is_directory=True)
        with self.assertRaises(ReleaseError):self.install(self.first)
        self.assertTrue((self.root/'server_setting').is_symlink())

    @unittest.skipUnless(os.name=='posix','Native atomic symlink transactions require POSIX')
    def test_stale_loaded_source_gate_uses_captured_physical_root(self):
        self.install(self.first);old=(self.root/'current').resolve()
        ensure_current_release(old)
        self.install(self.bundle('2026.10.25.002','b'*40))
        with self.assertRaises(ReleaseError) as error:ensure_current_release(old)
        self.assertEqual(error.exception.code,'UPDATE_RESTART_REQUIRED')

    @unittest.skipUnless(os.name=='posix','Native atomic symlink transactions require POSIX')
    def test_legacy_migration_and_code_rollback_preserve_config_and_original_program(self):
        source=self.root/'server_setting';source.mkdir(parents=True)
        for name in ('__init__.py','__main__.py'):(source/name).write_bytes(b'# legacy source\n')
        (self.root/'run.sh').write_bytes(b'#!/bin/sh\n# legacy launcher\n')
        (self.root/'requirements-tui.lock').write_bytes(b'# locked legacy fixture\n')
        private=self.root/'config/private.json';private.parent.mkdir();private.write_bytes(b'host secret')
        self.install(self.first)
        legacy=self.manager.status()['previous']
        self.assertTrue(legacy['legacy']);self.assertEqual(legacy['version'],'legacy')
        self.assertEqual((self.root/'release/legacy-flat-server_setting/__main__.py').read_bytes(),b'# legacy source\n')
        self.manager.rollback(self.manager.rollback_plan())
        self.assertEqual(self.manager.status()['installed']['version'],'legacy')
        self.assertEqual(private.read_bytes(),b'host secret')

    @unittest.skipUnless(os.name=='posix','Native atomic symlink transactions require POSIX')
    def test_signed_remote_preview_download_failure_and_no_change_preserve_current(self):
        self.install(self.first)
        second=self.bundle('2026.10.25.002','b'*40)
        tag=second['identity']['tag'];prefix='https://github.com/shadowjohn/3waServerSetup/releases/download/'+tag+'/'
        manifest=json.loads(second['manifest'].read_text())
        api={'tag_name':tag,'draft':False,'prerelease':False,'body':'Release fixture notes','assets':[
            {'name':name,'browser_download_url':prefix+name} for name in ('release-manifest.json','release-manifest.json.sig',manifest['archive']['name'])]}
        responses={'https://api.github.com/repos/shadowjohn/3waServerSetup/releases/latest':json.dumps(api).encode(),
            'https://api.github.com/repos/shadowjohn/3waServerSetup/releases/tags/'+tag:json.dumps(api).encode(),
            'https://api.github.com/repos/shadowjohn/3waServerSetup/commits/'+tag:json.dumps({'sha':'b'*40}).encode(),
            prefix+'release-manifest.json':second['manifest'].read_bytes(),prefix+'release-manifest.json.sig':second['signature'].read_bytes(),
            prefix+manifest['archive']['name']:second['archive'].read_bytes()}
        self.manager.fetcher=lambda url,limit:responses[url]
        plan=self.manager.plan();before=os.readlink(self.root/'current')
        del responses[prefix+manifest['archive']['name']]
        with self.assertRaises(ReleaseError) as error:self.manager.apply(plan)
        self.assertEqual(error.exception.code,'DOWNLOAD_FAILED')
        self.assertEqual(os.readlink(self.root/'current'),before)
        responses[prefix+manifest['archive']['name']]=second['archive'].read_bytes()
        result=self.manager.apply(plan);self.assertEqual(result['status'],'UPDATED')
        check=self.manager.check();self.assertEqual(check['status'],'UP_TO_DATE')
        self.assertEqual(self.manager.apply(self.manager.plan())['status'],'NO_CHANGE')

    def legacy_fixture(self):
        source=self.root/'server_setting';source.mkdir(parents=True)
        for name in ('__init__.py','__main__.py'):(source/name).write_bytes(b'# legacy source\n')
        (self.root/'run.sh').write_bytes(b'#!/bin/sh\n# legacy launcher\n')
        (self.root/'requirements-tui.lock').write_bytes(b'# locked legacy fixture\n')
        private=self.root/'config/private.json';private.parent.mkdir();private.write_bytes(b'host secret')
        return private

    @unittest.skipUnless(os.name=='posix','Native interruption uses POSIX symlinks')
    def test_legacy_alias_interruption_can_be_retried_with_the_verified_installer(self):
        private=self.legacy_fixture();original=Path.symlink_to
        def interrupted(path,target,*args,**kwargs):
            if path==self.root/'server_setting':raise KeyboardInterrupt('lost after legacy rename')
            return original(path,target,*args,**kwargs)
        with patch.object(Path,'symlink_to',interrupted):
            with self.assertRaises(KeyboardInterrupt):self.install(self.first)
        self.assertTrue((self.root/'release/legacy-flat-server_setting').exists())
        self.install(self.first)
        self.assertTrue((self.root/'server_setting').is_symlink())
        self.assertEqual(self.manager.status()['installed']['version'],'2026.10.25.001')
        self.assertEqual(private.read_bytes(),b'host secret')

    @unittest.skipUnless(os.name=='posix','Native interruption uses POSIX symlinks')
    def test_every_terminal_journal_write_can_be_retried_without_manual_state_edits(self):
        self.install(self.first);second=self.bundle('2026.10.25.002','b'*40)
        original=self.manager._atomic
        for failure in ('canonical','public','shared'):
            armed=[True]
            def interrupted(path,raw,mode=0o644):
                terminal=b'"status":"UPDATED"' in raw or b'"result":"UPDATED"' in raw or b'"pending":null' in raw
                matches=(failure=='canonical' and Path(path)==self.manager.journal_file
                    or failure=='public' and Path(path)==self.manager.control/'pending.json'
                    or failure=='shared' and Path(path).name=='journal.json' and self.manager.transactions in Path(path).parents)
                if armed[0] and terminal and matches:
                    armed[0]=False;raise KeyboardInterrupt('lost at terminal '+failure)
                return original(path,raw,mode)
            with self.subTest(failure=failure):
                with patch.object(self.manager,'_atomic',side_effect=interrupted):
                    with self.assertRaises(KeyboardInterrupt):self.install(second)
                self.install(second)
                self.assertIsNone(self.manager.status()['pending'])
                self.manager._pending_host()
                self.manager.rollback(self.manager.rollback_plan())

    @unittest.skipUnless(os.name=='posix','Legacy snapshots use POSIX symlinks')
    def test_failed_legacy_smoke_is_never_accepted_as_a_completed_snapshot(self):
        self.legacy_fixture();original=self.manager._smoke;attempts=[]
        def smoke(directory):
            if (directory/'server_setting/__main__.py').read_bytes()==b'# legacy source\n':
                attempts.append(str(directory));raise ReleaseError('fixture legacy cannot start','SMOKE_FAILED')
            return original(directory)
        with patch.object(self.manager,'_smoke',side_effect=smoke):
            for _ in range(2):
                with self.assertRaises(ReleaseError) as error:self.install(self.first)
                self.assertEqual(error.exception.code,'SMOKE_FAILED')
        self.assertEqual(len(attempts),2)
        self.assertFalse((self.root/'current').exists())
        self.assertFalse((self.root/'server_setting').is_symlink())

    @unittest.skipUnless(os.name=='posix','Durable switch ordering uses POSIX symlinks')
    def test_payload_is_durable_before_current_is_switched(self):
        synced=[];original=self.manager._point
        def durable(directory):synced.append(Path(directory))
        def point(name):
            self.assertTrue(synced,'The payload must be synced before the current pointer')
            return original(name)
        with patch.object(self.manager,'_durable_tree',side_effect=durable,create=True),patch.object(self.manager,'_point',side_effect=point):
            self.install(self.first)
        self.assertTrue(any(path.name.startswith('.staging-') for path in synced))

    @unittest.skipUnless(os.name=='posix','Native interruption uses POSIX symlinks')
    def test_shared_directory_and_initial_journal_interruptions_recover_without_an_orphan(self):
        self.install(self.first);second=self.bundle('2026.10.25.002','b'*40)
        directory=self.manager._dir;atomic=self.manager._atomic
        for failure in ('directory','journal'):
            armed=[True]
            def make(path,private=False):
                result=directory(path,private)
                if armed[0] and failure=='directory' and Path(path).parent==self.manager.transactions:
                    armed[0]=False;raise KeyboardInterrupt('lost after shared directory creation')
                return result
            def write(path,raw,mode=0o644):
                shared=Path(path).name=='journal.json' and self.manager.transactions in Path(path).parents
                if armed[0] and failure=='journal' and shared and b'UPDATE_SWITCHING' in raw:
                    armed[0]=False;raise KeyboardInterrupt('lost before first shared journal')
                return atomic(path,raw,mode)
            with self.subTest(failure=failure):
                with patch.object(self.manager,'_dir',side_effect=make),patch.object(self.manager,'_atomic',side_effect=write):
                    with self.assertRaises(KeyboardInterrupt):self.install(second)
                self.install(second)
                self.manager._pending_host()
                self.assertIsNone(self.manager.status()['pending'])
                self.manager.rollback(self.manager.rollback_plan())

    def test_an_unknown_empty_host_transaction_is_not_ignored(self):
        (self.manager.transactions/'foreign').mkdir(parents=True)
        with self.assertRaises(ReleaseError) as error:self.install(self.first)
        self.assertEqual(error.exception.code,'RECOVERY_REQUIRED')

    def test_python_preparation_does_not_import_from_the_callers_directory(self):
        candidate=self.base/'candidate';candidate.mkdir();(candidate/'wheelhouse').mkdir()
        (candidate/'requirements-tui.lock').write_text('threewa-missing-fixture==1 --hash=sha256:'+'0'*64+'\n',encoding='utf-8')
        hostile=self.base/'caller-download-directory';hostile.mkdir();marker=hostile/'executed'
        attack='from pathlib import Path\nPath('+repr(str(marker))+').write_text("unverified code")\nraise SystemExit(19)\n'
        for name in ('venv.py','pip.py'):(hostile/name).write_text(attack,encoding='utf-8')
        commands=[]
        def execute(argv,timeout=30,input=None,cwd=None):
            commands.append(list(argv))
            env=dict(os.environ)
            for key in ('PYTHONPATH','PYTHONHOME','PYTHONUSERBASE'):env.pop(key,None)
            # Keep the exact Python options while using this test interpreter on
            # both supported POSIX fixtures and Windows validation hosts.
            return subprocess.run([os.fspath(Path(os.sys.executable)),*argv[1:]],input=input,capture_output=True,
                                  timeout=timeout,cwd=cwd,env=env)
        self.manager.runner=execute;previous=Path.cwd()
        try:
            os.chdir(hostile)
            with self.assertRaises(ReleaseError):self.manager._make_venv(candidate)
        finally:os.chdir(previous)
        self.assertFalse(marker.exists(),'Python imported an unverified module from the invoking directory')
        self.assertEqual(len(commands),2,'Both venv and pip must run their genuine isolated module')

    @unittest.skipUnless(os.name=='posix','Stale updater source identity uses POSIX symlinks')
    def test_loaded_old_updater_cannot_apply_or_rollback_after_current_changes(self):
        from server_setting import release_update
        self.install(self.first);old=(self.root/'current').resolve()
        self.install(self.bundle('2026.10.25.002','b'*40))
        plan=self.manager.rollback_plan()
        with patch.object(release_update,'_RUNNING_CODE_ROOT',old):
            for action in (lambda:self.manager.apply({}),lambda:self.manager.rollback(plan)):
                with self.assertRaises(ReleaseError) as error:action()
                self.assertEqual(error.exception.code,'UPDATE_RESTART_REQUIRED')

    @unittest.skipUnless(os.name=='posix','Stale updater acquisition race uses POSIX symlinks')
    def test_updater_rechecks_captured_code_inside_the_acquired_lock(self):
        from contextlib import contextmanager
        from server_setting import release_update
        self.install(self.first);old=(self.root/'current').resolve()
        self.install(self.bundle('2026.10.25.002','b'*40));loaded=(self.root/'current').resolve()
        plan=self.manager.rollback_plan();original=self.manager._lock
        @contextmanager
        def raced():
            with original():
                self.manager._point(old.name)
                yield
        with patch.object(release_update,'_RUNNING_CODE_ROOT',loaded),patch.object(self.manager,'_lock',raced):
            with self.assertRaises(ReleaseError) as error:self.manager.rollback(plan)
        self.assertEqual(error.exception.code,'UPDATE_RESTART_REQUIRED')


if __name__=='__main__':unittest.main()

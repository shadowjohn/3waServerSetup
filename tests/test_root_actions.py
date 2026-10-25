"""Real temporary filesystem tests for privileged transactions."""
import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

try:
    from server_setting.root_actions import Host, RootManager, RootActionError
except ImportError:
    Host = RootManager = RootActionError = None


class RootActionsTests(unittest.TestCase):
    def test_completed_startup_transactions_allow_other_modules_and_running_still_blocks(self):
        for status in ('STARTUP_APPLIED', 'STARTUP_RESTORED', 'STARTUP_ROLLED_BACK',
                       'STARTUP_RUN_SUCCEEDED', 'STARTUP_RUN_FAILED'):
            identifier = self.manager._id()
            self.manager._journal({'schema': 1, 'transaction_id': identifier,
                                  'module': 'startup', 'kind': 'startup', 'status': status})
            self.manager._check_pending()
        identifier = self.manager._id()
        self.manager._journal({'schema': 1, 'transaction_id': identifier,
                              'module': 'startup', 'kind': 'startup', 'status': 'STARTUP_RUNNING'})
        with self.assertRaises(RootActionError) as failure:
            self.manager._check_pending()
        self.assertEqual(failure.exception.code, 'RECOVERY_REQUIRED')

    def test_completed_banip_transactions_do_not_block_other_modules_but_pending_does(self):
        for status in ('BANIP_CONFIRMED', 'BANIP_ROLLED_BACK', 'BANIP_FAILED'):
            identifier = self.manager._id()
            self.manager._journal({'schema':1,'transaction_id':identifier,'module':'banip','kind':'banip','status':status})
            self.manager._check_pending()
        identifier = self.manager._id()
        self.manager._journal({'schema':1,'transaction_id':identifier,'module':'banip','kind':'banip','status':'BANIP_AWAITING_NEW_SSH'})
        with self.assertRaises(RootActionError) as failure:
            self.manager._check_pending()
        self.assertEqual(failure.exception.code,'RECOVERY_REQUIRED')

    def setUp(self):
        self.assertIsNotNone(RootManager, 'Root transaction engine is not implemented')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'etc').mkdir()
        (self.root / 'etc/os-release').write_text('ID=ubuntu\nVERSION_ID="26.04"\n', encoding='utf-8')
        (self.root / 'etc/example.conf').write_bytes(b'# original\r\nsecret=do-not-log\xff\r\n')
        self.original = (self.root / 'etc/example.conf').read_bytes()
        self.calls = []
        outer = self
        class FakeHost(Host):
            def run(self, argv, timeout=30):
                outer.calls.append(list(argv))
                if argv == ['check-config'] and outer.fail_validation and (outer.root / 'etc/example.conf').read_bytes() != outer.original:
                    return subprocess.CompletedProcess(argv, 1, 'secret-output', 'secret-error')
                return subprocess.CompletedProcess(argv, 0, '', '')
        self.host = FakeHost(self.root)
        self.fail_validation = False
        self.active = True
        self.enabled = True
        class Recipes:
            @staticmethod
            def packages(module):
                return ['apache2']
            @staticmethod
            def capabilities(host, module):
                return {'packages': ['apache2'], 'installed': True, 'supported': True,
                        'service': {'unit': 'apache2.service', 'active': outer.active, 'enabled': outer.enabled}}
            @staticmethod
            def build_plan(host, module, values, service_active=None, service_enabled=None):
                path = host.path('/etc/example.conf')
                data = path.read_bytes() if path.exists() else None
                return {'schema': 1, 'module': module, 'values': values,
                        'service_active': service_active, 'service_enabled': service_enabled,
                        'files': [{'path': '/etc/example.conf', 'before_sha256': hashlib.sha256(data).hexdigest() if data is not None else None,
                                   'content': 'ServerTokens=' + values['ServerTokens'] + '\nServerSignature=' + values['ServerSignature'] + '\n', 'mode': 0o640}],
                        'validate': [['check-config']], 'apply_commands': [['systemctl', 'reload', 'apache2.service']],
                        'restore_commands': [], 'observations': {}, 'notes': [], 'health_scope': 'configuration only',
                        'service_snapshot': {'unit': 'apache2.service', 'active': outer.active, 'enabled': outer.enabled}}
            @staticmethod
            def verify_plan(host, plan):
                return {'verified': True, 'scope': 'configuration only', 'detail': 'checked'}
            @staticmethod
            def post_install(host, module):
                return {'detail': 'no integration required'}
        self.recipes = Recipes
        self.manager = RootManager(self.host)
        self.addCleanup(patch.stopall)
        patch.object(self.manager, '_recipes', return_value=Recipes).start()
        patch('server_setting.root_actions.os.geteuid', return_value=0, create=True).start()
        @contextmanager
        def fake_lock():
            yield
        patch.object(self.manager, '_lock', fake_lock).start()

    def test_startup_and_recommendation_are_observational(self):
        self.assertEqual(self.manager.recommended('apache'), {'ServerTokens': 'Prod', 'ServerSignature': 'Off'})
        self.assertIsNone(self.manager.load_saved('apache'))
        self.assertFalse((self.root / 'opt').exists())
        self.assertFalse((self.root / 'var').exists())

    def test_native_os_release_symlink_is_allowed_only_at_the_platform_boundary(self):
        canonical = self.root / 'usr/lib/os-release'
        canonical.parent.mkdir(parents=True)
        canonical.write_text('ID=ubuntu\nVERSION_ID="26.04"\n')
        native = self.root / 'etc/os-release'
        is_symlink = type(native).is_symlink
        readlink = type(native).readlink
        for target in ('../usr/lib/os-release', '/usr/lib/os-release', '../usr/lib/not-os-release'):
            # Windows needs elevated symlink creation; replace only that OS boundary.
            with patch.object(type(native), 'is_symlink', lambda path: path == native or is_symlink(path)), \
                    patch.object(type(native), 'readlink', lambda path: Path(target) if path == native else readlink(path)):
                if target.endswith('/not-os-release'):
                    with self.assertRaises(RootActionError):
                        self.manager._platform()
                else:
                    self.assertTrue(self.manager._platform())
                with self.assertRaises(RootActionError):
                    self.host.read('/etc/os-release')

    def test_nonroot_and_unsupported_os_refuse_before_state_creation(self):
        plan = self.manager.plan('apache')
        with patch('server_setting.root_actions.os.geteuid', return_value=1000):
            with self.assertRaises(RootActionError):
                self.manager.apply(plan)
        (self.root / 'etc/os-release').write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
        with self.assertRaises(RootActionError) as raised:
            self.manager.apply(plan)
        self.assertEqual(raised.exception.code, 'UNSUPPORTED_OS')
        self.assertFalse((self.root / 'opt').exists())
        self.assertFalse((self.root / 'var').exists())

    def test_plan_rejects_partial_unknown_and_invalid_values(self):
        for values in ({'ServerTokens': 'Prod'}, {'ServerTokens': 'Prod', 'ServerSignature': 'Off', 'other': 'x'}, {'ServerTokens': 'Prod\nInjected On', 'ServerSignature': 'Off'}):
            with self.subTest(values=values), self.assertRaises((ValueError, RootActionError)):
                self.manager.plan('apache', values)
        with self.assertRaises(RootActionError):
            self.manager.plan('storage')

    def test_apply_backups_exact_bytes_and_restore(self):
        plan = self.manager.plan('apache')
        self.assertEqual(plan['fingerprint'], self.manager.plan('apache')['fingerprint'])
        result = self.manager.apply(plan)
        self.assertEqual(result['status'], 'APPLIED')
        transaction = result['transaction_id']
        backup = self.root / 'opt/3waServerSetting/backups' / transaction / '0000.bin'
        self.assertEqual(backup.read_bytes(), self.original)
        saved = self.manager.load_saved('apache')
        self.assertEqual(saved['values'], plan['values'])
        journal = (self.root / 'var/lib/3wa-server-setup/transactions' / transaction / 'journal.json').read_text()
        self.assertNotIn('do-not-log', journal)
        self.assertNotIn('secret-output', journal)
        restored = self.manager.restore_last('apache')
        self.assertEqual(restored['status'], 'RESTORED')
        self.assertEqual((self.root / 'etc/example.conf').read_bytes(), self.original)

    def test_stale_and_tampered_plan_never_write(self):
        plan = self.manager.plan('apache')
        tampered = copy.deepcopy(plan)
        tampered['apply_commands'] = [['sh', '-c', 'evil']]
        with self.assertRaises(RootActionError):
            self.manager.apply(tampered)
        (self.root / 'etc/example.conf').write_bytes(b'external\n')
        with self.assertRaises(RootActionError):
            self.manager.apply(plan)
        self.assertEqual((self.root / 'etc/example.conf').read_bytes(), b'external\n')
        self.assertFalse(self.calls)

    def test_rehashed_caller_plan_cannot_change_trusted_paths_or_commands(self):
        plan = self.manager.plan('apache')
        plan['files'][0]['path'] = '/etc/unapproved.conf'
        plan['apply_commands'] = [['sh', '-c', 'evil']]
        plan['fingerprint'] = hashlib.sha256(json.dumps({key: value for key, value in plan.items() if key != 'fingerprint'},
                                            sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        with self.assertRaises(RootActionError):
            self.manager.apply(plan)
        self.assertFalse((self.root / 'etc/unapproved.conf').exists())
        self.assertEqual((self.root / 'etc/example.conf').read_bytes(), self.original)
        self.assertFalse(self.calls)

    def test_checked_failure_restores_bytes_and_service_state_without_raw_output(self):
        self.fail_validation = True
        plan = self.manager.plan('apache', service_active=False, service_enabled=False)
        with self.assertRaises(RootActionError) as raised:
            self.manager.apply(plan)
        self.assertNotIn('secret-output', str(raised.exception))
        self.assertEqual((self.root / 'etc/example.conf').read_bytes(), self.original)
        self.assertIn(['systemctl', 'enable', 'apache2.service'], self.calls)
        self.assertIn(['systemctl', 'start', 'apache2.service'], self.calls)
        self.assertEqual(self.calls.count(['check-config']), 2)

    def test_restore_refuses_external_edit(self):
        self.manager.apply(self.manager.plan('apache'))
        (self.root / 'etc/example.conf').write_bytes(b'manual change')
        with self.assertRaises(RootActionError):
            self.manager.restore_last('apache')
        self.assertEqual((self.root / 'etc/example.conf').read_bytes(), b'manual change')

    def test_rollback_never_overwrites_an_external_edit_during_validation(self):
        edited = False
        def fail_after_external_edit(argv, timeout=30):
            nonlocal edited
            if not edited:
                edited = True
                (self.root / 'etc/example.conf').write_bytes(b'external concurrent edit')
                raise RootActionError('checked failure')
            return subprocess.CompletedProcess(argv, 0, '', '')
        with patch.object(self.manager, '_checked', side_effect=fail_after_external_edit):
            with self.assertRaises(RootActionError) as raised:
                self.manager.apply(self.manager.plan('apache'))
        self.assertEqual(raised.exception.code, 'RECOVERY_REQUIRED')
        self.assertEqual((self.root / 'etc/example.conf').read_bytes(), b'external concurrent edit')

    def test_noop_does_not_reload_or_create_transaction(self):
        self.manager.apply(self.manager.plan('apache'))
        self.calls.clear()
        before = sorted((self.root / 'var/lib/3wa-server-setup/transactions').iterdir())
        result = self.manager.apply(self.manager.plan('apache'))
        self.assertEqual(result['status'], 'NO_CHANGE')
        self.assertNotIn(['systemctl', 'reload', 'apache2.service'], self.calls)
        self.assertEqual(before, sorted((self.root / 'var/lib/3wa-server-setup/transactions').iterdir()))

    def test_saved_target_only_change_is_reversible_without_reloading_service(self):
        self.manager.apply(self.manager.plan('apache'))
        prior = self.manager.load_saved('apache')
        self.calls.clear()
        result = self.manager.apply(self.manager.plan('apache', service_active=True))
        self.assertIn('transaction_id', result)
        self.assertNotIn(['systemctl', 'reload', 'apache2.service'], self.calls)
        self.assertNotIn(['systemctl', 'restart', 'apache2.service'], self.calls)
        self.assertIs(self.manager.load_saved('apache')['service_active'], True)
        self.manager.restore_last('apache')
        self.assertEqual(self.manager.load_saved('apache'), prior)
        self.manager.restore_last('apache')
        self.assertEqual((self.root / 'etc/example.conf').read_bytes(), self.original)

    def test_log_path_failure_is_detected_before_native_configuration_changes(self):
        from server_setting.root_actions import LOGS
        original = self.manager._secure_dir
        def reject_logs(path):
            if path == LOGS:
                raise RootActionError('unsafe log path')
            return original(path)
        with patch.object(self.manager, '_secure_dir', side_effect=reject_logs):
            with self.assertRaises(RootActionError):
                self.manager.apply(self.manager.plan('apache'))
        self.assertEqual((self.root / 'etc/example.conf').read_bytes(), self.original)

    def test_final_log_failure_reports_applied_with_warning_and_keeps_restore_available(self):
        with patch.object(self.manager, '_log', side_effect=OSError('log write unavailable')):
            result = self.manager.apply(self.manager.plan('apache'))
        self.assertEqual(result['status'], 'APPLIED_WITH_LOG_WARNING')
        self.assertIn('log', result['message'].lower())
        self.assertEqual(self.manager.restore_last('apache')['status'], 'RESTORED')

    def test_plan_rejects_writable_native_parent_without_changing_permissions(self):
        original_stat = Path.stat
        native_parent = self.root / 'etc'
        before = native_parent.stat().st_mode
        if os.name == 'posix':
            unsafe = before | 0o020
            os.chmod(native_parent, unsafe)
            try:
                with self.assertRaises(RootActionError):
                    self.manager.plan('apache')
                self.assertEqual(native_parent.stat().st_mode, unsafe)
            finally:
                os.chmod(native_parent, before)
            return
        def metadata(path, *args, **kwargs):
            info = original_stat(path, *args, **kwargs)
            return SimpleNamespace(st_mode=(info.st_mode & ~0o022) | (0o020 if path == native_parent else 0),
                                   st_uid=0, st_gid=0)
        with patch('server_setting.root_actions._POSIX_PERMISSIONS', True, create=True), \
                patch.object(Path, 'stat', metadata), patch.object(Path, 'lstat', metadata):
            with self.assertRaises(RootActionError):
                self.manager.plan('apache')
        self.assertEqual(native_parent.stat().st_mode, before)

    def test_restore_summary_displays_previous_timezone(self):
        summary = self.manager._restore_summary({'schema': 1, 'module': 'time', 'transaction_id': 'example',
                    'files': [], 'service_snapshot': {'unit': None},
                    'restore_commands': [['timedatectl', 'set-timezone', 'Etc/UTC']],
                    'observations': {'previous_timezone': 'Etc/UTC'}})
        self.assertEqual(summary['observations']['previous_timezone'], 'Etc/UTC')
        self.assertEqual(summary['restore_commands'], [['timedatectl', 'set-timezone', 'Etc/UTC']])

    def test_host_rejects_traversal_and_symlink(self):
        for path in ('relative', '/etc/../escape', '/etc/./example.conf', '/etc\\example.conf'):
            with self.subTest(path=path), self.assertRaises(RootActionError):
                self.host.path(path)
        try:
            (self.root / 'etc/link').symlink_to(self.root / 'etc/example.conf')
        except OSError:
            return
        with self.assertRaises(RootActionError):
            self.host.read('/etc/link')

    def test_missing_flock_fails_closed(self):
        with patch('server_setting.root_actions._fcntl', None):
            with self.assertRaises(RootActionError):
                with RootManager(self.host)._lock():
                    self.fail('must not acquire unsupported lock')
        self.assertFalse((self.root / 'run').exists())

    @unittest.skipUnless(os.name=='posix','The acquisition race uses native flock and symlinks')
    def test_release_gate_is_rechecked_inside_the_acquired_root_lock(self):
        from server_setting import root_actions
        installation=self.root/'installation';old=installation/'releases/old';new=installation/'releases/new'
        old.mkdir(parents=True);new.mkdir()
        pointer=installation/'current';pointer.symlink_to('releases/old',target_is_directory=True)
        manager=RootManager(self.host)
        original=root_actions._fcntl.flock
        def acquire_then_update(descriptor, flags):
            original(descriptor, flags)
            changed=installation/'next';changed.symlink_to('releases/new',target_is_directory=True)
            os.replace(changed,pointer)
        with patch.object(root_actions,'_RUNNING_CODE_ROOT',old):
            manager._gate('apache')  # Valid before the updater switches current.
            with patch.object(root_actions._fcntl,'flock',side_effect=acquire_then_update):
                with self.assertRaises(RootActionError) as error:
                    with manager._lock():self.fail('Stale code acquired permission to mutate')
            self.assertEqual(error.exception.code,'UPDATE_RESTART_REQUIRED')

    def test_install_only_explicit_packages_never_full_upgrade(self):
        plan = self.manager.install_plan('apache')
        self.assertEqual(plan['packages'], ['apache2'])
        result = self.manager.install('apache')
        self.assertEqual(result['status'], 'INSTALLED')
        self.assertIn(['apt-get', 'install', '--yes', '--no-install-recommends', 'apache2'], self.calls)
        self.assertFalse(any('upgrade' in call or 'autoremove' in call for call in self.calls))

    def test_restore_recovers_previous_saved_recipe(self):
        self.manager.apply(self.manager.plan('apache'))
        prior = self.manager.load_saved('apache')
        custom = {'ServerTokens': 'Major', 'ServerSignature': 'On'}
        self.manager.apply(self.manager.plan('apache', custom))
        self.manager.restore_last('apache')
        self.assertEqual(self.manager.load_saved('apache'), prior)

    def test_latest_transaction_uses_creation_order_not_random_identifier(self):
        identifiers = ['20261024T120000Z-ffffffffffff', '20261024T120000Z-000000000000']
        with patch.object(self.manager, '_id', side_effect=identifiers):
            self.manager.apply(self.manager.plan('apache'))
            first = (self.root / 'etc/example.conf').read_bytes()
            self.manager.apply(self.manager.plan('apache', {'ServerTokens': 'Major', 'ServerSignature': 'On'}))
        self.manager.restore_last('apache')
        self.assertEqual((self.root / 'etc/example.conf').read_bytes(), first)

    def test_incomplete_transaction_blocks_further_mutation(self):
        result = self.manager.apply(self.manager.plan('apache'))
        path = self.root / 'var/lib/3wa-server-setup/transactions' / result['transaction_id'] / 'journal.json'
        journal = json.loads(path.read_text())
        journal['status'] = 'APPLYING'
        path.write_text(json.dumps(journal))
        with self.assertRaises(RootActionError) as raised:
            self.manager.apply(self.manager.plan('apache'))
        self.assertEqual(raised.exception.code, 'RECOVERY_REQUIRED')

    def test_managed_parent_is_not_silently_created_or_chmodded(self):
        path = '/etc/missing/example.conf'
        with self.assertRaises(RootActionError):
            self.manager._atomic(path, b'config', {'mode': 0o644, 'uid': 0, 'gid': 0, 'xattrs': {}})
        self.assertFalse((self.root / 'etc/missing').exists())

    def test_restore_preview_is_readonly_and_does_not_expose_original_content(self):
        self.manager.apply(self.manager.plan('apache'))
        self.calls.clear()
        result = self.manager.restore_plan('apache')
        self.assertEqual(result['module'], 'apache')
        self.assertEqual(result['files'][0]['path'], '/etc/example.conf')
        self.assertNotIn('do-not-log', json.dumps(result))
        self.assertFalse(self.calls)

    def test_restore_refuses_modified_saved_recipe(self):
        self.manager.apply(self.manager.plan('apache'))
        path = self.root / 'opt/3waServerSetting/config/recipes/apache.json'
        saved = json.loads(path.read_text())
        saved['values']['ServerTokens'] = 'Major'
        path.write_text(json.dumps(saved))
        with self.assertRaises(RootActionError):
            self.manager.restore_last('apache')

    def test_restore_refuses_a_stale_review_fingerprint(self):
        self.manager.apply(self.manager.plan('apache'))
        current = (self.root / 'etc/example.conf').read_bytes()
        with self.assertRaises(RootActionError):
            self.manager.restore_last('apache', expected_fingerprint='modified')
        self.assertEqual((self.root / 'etc/example.conf').read_bytes(), current)

    def test_plan_refuses_nonreversible_enablement_state(self):
        original = self.recipes.build_plan
        def masked(*args, **kwargs):
            plan = original(*args, **kwargs)
            plan['service_snapshot']['unit_file_state'] = 'masked'
            return plan
        with patch.object(self.recipes, 'build_plan', side_effect=masked):
            with self.assertRaises(RootActionError):
                self.manager.plan('apache')

    def test_metadata_change_invalidates_preview(self):
        plan = self.manager.plan('apache')
        os.chmod(self.root / 'etc/example.conf', 0o444)
        with self.assertRaises(RootActionError) as raised:
            self.manager.apply(plan)
        self.assertEqual(raised.exception.code, 'STALE_PLAN')

    def test_restore_preserves_original_metadata(self):
        target = self.root / 'etc/example.conf'
        os.chmod(target, 0o640)
        before = target.stat()
        result = self.manager.apply(self.manager.plan('apache'))
        self.manager.restore_last('apache')
        after = target.stat()
        self.assertEqual((after.st_mode, after.st_uid, after.st_gid), (before.st_mode, before.st_uid, before.st_gid))
        backup = self.root / 'opt/3waServerSetting/backups' / result['transaction_id'] / '0000.bin'
        if os.name == 'posix':
            self.assertEqual(backup.stat().st_mode & 0o777, (before.st_mode & 0o600))


class HostRunnerTests(unittest.TestCase):
    def test_runner_rejects_shell_and_untrusted_executable(self):
        self.assertIsNotNone(Host, 'Root transaction engine is not implemented')
        host = Host()
        for argv in (['sh', '-c', 'true'], ['/tmp/systemctl', 'start', 'apache2'], 'systemctl start apache2'):
            with self.subTest(argv=argv), self.assertRaises(RootActionError):
                host.run(argv)


if __name__ == '__main__':
    unittest.main()

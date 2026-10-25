"""Startup transactions use real temporary files and a substituted systemd boundary."""
import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from server_setting.root_actions import Host, RootActionError
try:
    from server_setting.root_startup import StartupManager, default_config
except ImportError:
    StartupManager = default_config = None


SCRIPT = '/opt/3waServerSetting/config/startup/startup.sh'
UNIT = '/etc/systemd/system/threewa-startup.service'
OWNED = '/var/lib/3wa-server-setup/startup/owned.json'
UNIT_NAME = 'threewa-startup.service'


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(StartupManager, 'Startup backend is missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'etc/systemd/system').mkdir(parents=True)
        (self.root / 'etc/os-release').write_text('ID=ubuntu\nVERSION_ID="26.04"\n')
        (self.root / 'proc/sys/kernel/random').mkdir(parents=True)
        (self.root / 'proc/sys/kernel/random/boot_id').write_text('11111111-1111-1111-1111-111111111111\n')
        self.events = []; self.enabled = False; self.active = 'inactive'; self.sub = 'dead'
        self.result = 'success'; self.exit_status = 0; self.invocation = ''
        self.dropins = ''; self.fail_enable = False; self.fail_run = False
        self.timeout_run = False; self.interrupt = False; self.unit_loaded = False
        self.extra_links = []; self.run_number = 0
        self.canary = self.root / 'executed.txt'
        outer = self

        class FakeHost(Host):
            def startup_links(self):
                return ([{'path': '/etc/systemd/system/multi-user.target.wants/' + UNIT_NAME,
                          'target': UNIT, 'uid': 0}] if outer.enabled else []) + outer.extra_links

            def run(self, argv, timeout=30):
                outer.events.append(list(argv))
                text = ''; code = 0
                if argv[0] == 'bash':
                    path = Path(argv[-1])
                    if b'BROKEN SYNTAX' in path.read_bytes(): code = 2
                elif argv[0] == 'systemd-analyze':
                    if b'INVALID UNIT' in Path(argv[-1]).read_bytes(): code = 1
                elif argv[:2] == ['systemctl', 'show']:
                    if argv[2] == 'rc-local.service':
                        text = 'LoadState=not-found\nActiveState=inactive\n'
                    else:
                        present = self.path(UNIT).exists()
                        loaded = present or outer.unit_loaded
                        values = {'LoadState': 'loaded' if loaded else 'not-found',
                            'ActiveState': outer.active, 'SubState': outer.sub,
                            'UnitFileState': 'enabled' if outer.enabled else ('disabled' if loaded else ''),
                            'FragmentPath': UNIT if loaded else '', 'DropInPaths': outer.dropins,
                            'Transient': 'no', 'Result': outer.result, 'ExecMainStatus': str(outer.exit_status),
                            'ExecMainCode': '1' if outer.invocation else '0', 'InvocationID': outer.invocation,
                            'ExecMainStartTimestampMonotonic': '100' if outer.invocation else '0',
                            'ExecMainExitTimestampMonotonic': '200' if outer.invocation and outer.active != 'activating' else '0',
                            'ExecStart': '{ path=/usr/bin/env ; argv[]=/usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root LANG=C LC_ALL=C /bin/bash --noprofile --norc ' + SCRIPT + ' ; ignore_errors=no ; }' if loaded else ''}
                        text = ''.join(f'{key}={value}\n' for key, value in values.items())
                elif argv[:2] == ['systemctl', 'daemon-reload']:
                    outer.unit_loaded = self.path(UNIT).exists() or outer.active == 'active'
                elif argv[:2] == ['systemctl', 'enable']:
                    if outer.fail_enable:
                        outer.fail_enable = False; code = 1
                    else: outer.enabled = True
                elif argv[:2] == ['systemctl', 'disable']:
                    outer.enabled = False
                elif argv[:2] == ['systemctl', 'start']:
                    outer.unit_loaded = True; outer.run_number += 1; outer.invocation = f'{outer.run_number:032x}'
                    outer.canary.write_text(outer.canary.read_text() + 'run\n' if outer.canary.exists() else 'run\n')
                    if outer.interrupt:
                        raise KeyboardInterrupt()
                    if outer.timeout_run:
                        outer.active = 'failed'; outer.sub = 'failed'; outer.result = 'timeout'; outer.exit_status = 15
                        raise RootActionError('native timeout with secret output', 'TIMEOUT')
                    if outer.fail_run:
                        outer.active = 'failed'; outer.sub = 'failed'; outer.result = 'exit-code'; outer.exit_status = 7; code = 1
                    else: outer.active = 'active'; outer.sub = 'exited'; outer.result = 'success'; outer.exit_status = 0
                else: raise AssertionError(argv)
                return subprocess.CompletedProcess(argv, code, text, 'SENSITIVE native diagnostic' if code else '')

        self.host = FakeHost(self.root)
        self.manager = StartupManager(self.host)
        self.addCleanup(patch.stopall)
        patch('server_setting.root_actions.os.geteuid', return_value=0, create=True).start()
        patch('server_setting.root_startup.time.monotonic_ns', return_value=100000).start()
        @contextmanager
        def unlocked(): yield
        patch.object(self.manager._root, '_lock', unlocked).start()

    def config(self, **extra):
        result = default_config()
        result['script'] = '#!/bin/bash\nprintf "private-secret-value"\n'
        result.update(extra)
        return result

    def path(self, absolute):
        return self.host.path(absolute)

    def assert_no_execution(self):
        self.assertFalse(self.canary.exists())
        self.assertFalse(any(len(event) > 1 and event[1] in {'start', 'stop', 'restart', 'reload-or-restart'} for event in self.events))

    def test_status_save_and_preview_are_nonexecuting(self):
        self.assertFalse(self.manager.status()['managed'])
        self.assertIsNone(self.manager.load_current())
        self.assertIsNone(self.manager.load_saved())
        self.assertFalse(self.path('/var').exists())
        config = self.config(script='#!/bin/bash\r\necho hello\r\n')
        self.assertEqual(self.manager.save(config)['status'], 'SAVED')
        self.assertEqual(self.manager.load_saved()['script'], '#!/bin/bash\necho hello\n')
        preview = self.manager.plan(self.manager.load_saved())
        self.assertEqual(preview['module'], 'startup')
        self.assertFalse(self.path(SCRIPT).exists())
        self.assert_no_execution()

    def test_apply_enables_boot_without_running_and_noop_does_not_reload(self):
        result = self.manager.apply(self.manager.plan(self.config(enabled=True)))
        self.assertEqual(result['status'], 'STARTUP_APPLIED')
        self.assertTrue(self.enabled)
        self.assertEqual(self.manager.load_current(), self.config(enabled=True))
        self.assert_no_execution()
        self.assertIn('RemainAfterExit=yes', self.path(UNIT).read_text())
        self.assertIn('TimeoutStartSec=120', self.path(UNIT).read_text())
        before = list(self.events)
        self.assertEqual(self.manager.apply(self.manager.plan(self.config(enabled=True)))['status'], 'NO_CHANGE')
        self.assertFalse(any(event[:2] == ['systemctl', 'daemon-reload'] for event in self.events[len(before):]))

    def test_schema_syntax_and_timeout_are_checked_before_live_writes(self):
        invalid = [self.config(timeout_seconds=True), self.config(timeout_seconds=0), self.config(timeout_seconds=601),
                   self.config(script='\ufeffecho bad'), self.config(script='x\x00x'), self.config(script='x'*65537),
                   dict(self.config(), unexpected=True), self.config(enabled=1)]
        for config in invalid:
            with self.subTest(config={k:v for k,v in config.items() if k != 'script'}):
                with self.assertRaises(RootActionError): self.manager.plan(config)
        with self.assertRaises(RootActionError): self.manager.apply(self.manager.plan(self.config(script='BROKEN SYNTAX')))
        self.assertFalse(self.path(SCRIPT).exists())
        self.assert_no_execution()

    def test_unknown_unit_dropin_symlink_transition_and_stale_metadata_refuse(self):
        self.path(UNIT).write_text('[Service]\nExecStart=/bin/false\n')
        with self.assertRaises(RootActionError): self.manager.plan(self.config())
        self.path(UNIT).unlink()
        self.manager.apply(self.manager.plan(self.config()))
        self.dropins = '/etc/systemd/system/threewa-startup.service.d/external.conf'
        with self.assertRaises(RootActionError): self.manager.plan(self.config())
        self.dropins = ''; self.active = 'activating'
        with self.assertRaises(RootActionError): self.manager.apply(self.manager.plan(self.config()))
        self.active = 'inactive'
        preview = self.manager.plan(self.config(enabled=True))
        self.path(SCRIPT).write_bytes(self.path(SCRIPT).read_bytes() + b'# external\n')
        with self.assertRaises(RootActionError): self.manager.apply(preview)
        self.assertIn(b'external', self.path(SCRIPT).read_bytes())

    def test_apply_failure_restores_original_configuration_without_starting_or_stopping(self):
        self.manager.apply(self.manager.plan(self.config()))
        original = self.path(SCRIPT).read_bytes(); ownership = self.path(OWNED).read_bytes()
        self.fail_enable = True
        with self.assertRaises(RootActionError): self.manager.apply(self.manager.plan(self.config(script='#!/bin/bash\necho changed\n', enabled=True)))
        self.assertEqual(self.path(SCRIPT).read_bytes(), original)
        self.assertEqual(self.path(OWNED).read_bytes(), ownership)
        self.assertFalse(self.enabled)
        self.assert_no_execution()

    def test_run_is_separate_and_active_repeat_does_not_execute_again(self):
        self.manager.apply(self.manager.plan(self.config()))
        preview = self.manager.run_plan()
        self.assertNotIn('private-secret-value', json.dumps(preview))
        self.assertEqual(self.manager.run_once(preview)['status'], 'STARTUP_RUN_SUCCEEDED')
        self.assertEqual(self.canary.read_text(), 'run\n')
        self.assertEqual(self.manager.run_once(self.manager.run_plan())['status'], 'NO_CHANGE')
        self.assertEqual(self.canary.read_text(), 'run\n')

    def test_run_failure_and_timeout_never_retry_or_expose_native_output(self):
        self.manager.apply(self.manager.plan(self.config()))
        self.fail_run = True
        result = self.manager.run_once(self.manager.run_plan())
        self.assertEqual(result['status'], 'STARTUP_RUN_FAILED')
        self.assertEqual(self.canary.read_text(), 'run\n')
        self.active = 'inactive'; self.sub = 'dead'; self.fail_run = False; self.timeout_run = True
        self.assertEqual(self.manager.run_once(self.manager.run_plan())['status'], 'STARTUP_RUN_FAILED')
        self.assertEqual(self.canary.read_text(), 'run\nrun\n')
        self.assertFalse(any(event[1] in {'stop', 'restart'} for event in self.events if event[0] == 'systemctl'))
        for folder in ('var/lib/3wa-server-setup/transactions', 'var/lib/3wa-server-setup/logs'):
            for path in (self.root/folder).rglob('*.json'):
                self.assertNotIn('private-secret-value', path.read_text())
                self.assertNotIn('SENSITIVE', path.read_text())

    def test_restore_absent_keeps_active_runtime_and_tombstone_supports_next_apply(self):
        self.manager.apply(self.manager.plan(self.config(enabled=True)))
        self.manager.run_once(self.manager.run_plan())
        result = self.manager.restore(self.manager.restore_plan())
        self.assertEqual(result['status'], 'STARTUP_RESTORED')
        self.assertFalse(self.path(SCRIPT).exists()); self.assertFalse(self.path(UNIT).exists())
        self.assertEqual(self.active, 'active'); self.assertFalse(self.enabled)
        self.assertIsNone(self.manager.load_current())
        self.assertTrue(self.manager.status()['supported'])
        self.manager.apply(self.manager.plan(self.config()))
        self.assertEqual(self.manager.run_once(self.manager.run_plan())['status'], 'NO_CHANGE')
        self.assertEqual(self.canary.read_text(), 'run\n')

    def test_interrupted_normal_restore_after_tombstone_recovers_prior_configuration(self):
        self.manager.apply(self.manager.plan(self.config(enabled=True)))
        original = self.manager._root._private_json
        def interrupted(absolute, data):
            original(absolute, data)
            if absolute == OWNED and not data['present']: raise KeyboardInterrupt()
        with patch.object(self.manager._root, '_private_json', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt): self.manager.restore(self.manager.restore_plan())
        self.assertFalse(self.path(SCRIPT).exists())
        self.assertTrue(self.manager.restore_plan()['recovery'])
        self.assertEqual(self.manager.restore(self.manager.restore_plan())['status'], 'STARTUP_ROLLED_BACK')
        self.assertEqual(self.manager.load_current(), self.config(enabled=True))
        self.assertTrue(self.enabled)
        self.assert_no_execution()

    def test_load_current_reports_live_boot_enablement_without_changing_it(self):
        self.manager.apply(self.manager.plan(self.config(enabled=True)))
        self.enabled = False
        self.assertFalse(self.manager.load_current()['enabled'])
        self.assertFalse(self.enabled)
        self.assert_no_execution()

    def test_interrupted_run_recovery_only_observes_original_result(self):
        self.manager.apply(self.manager.plan(self.config()))
        self.interrupt = True
        with self.assertRaises(KeyboardInterrupt): self.manager.run_once(self.manager.run_plan())
        self.assertIsNotNone(self.manager.status()['pending'])
        self.active = 'active'; self.sub = 'exited'
        preview = self.manager.restore_plan()
        self.assertTrue(preview['recovery'])
        self.assertEqual(self.manager.restore(preview)['status'], 'STARTUP_RUN_SUCCEEDED')
        self.assertEqual(self.canary.read_text(), 'run\n')
        self.manager._root._check_pending()

    def test_unknown_extra_enablement_is_rejected_before_any_live_change(self):
        self.manager.apply(self.manager.plan(self.config(enabled=True)))
        before = self.path(SCRIPT).read_bytes()
        self.extra_links = [{'path': '/etc/systemd/system/extra.target.wants/' + UNIT_NAME, 'target': UNIT, 'uid': 0}]
        with self.assertRaises(RootActionError): self.manager.plan(self.config(enabled=False))
        self.assertEqual(self.path(SCRIPT).read_bytes(), before)
        self.assertTrue(self.enabled)
        self.assert_no_execution()

    def test_interrupted_config_and_interrupted_rollback_are_recoverable_without_run(self):
        original_atomic = self.manager._root._atomic
        def interrupted(absolute, data, metadata):
            original_atomic(absolute, data, metadata)
            if absolute == SCRIPT: raise KeyboardInterrupt()
        with patch.object(self.manager._root, '_atomic', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt): self.manager.apply(self.manager.plan(self.config(enabled=True)))
        self.assertIsNotNone(self.manager.status()['pending'])
        preview = self.manager.restore_plan()
        original_json = self.manager._root._private_json
        def interrupted_tombstone(absolute, data):
            original_json(absolute, data)
            if absolute == OWNED and not data['present']: raise KeyboardInterrupt()
        with patch.object(self.manager._root, '_private_json', side_effect=interrupted_tombstone):
            with self.assertRaises(KeyboardInterrupt): self.manager.restore(preview)
        self.assertEqual(self.manager.restore(self.manager.restore_plan())['status'], 'STARTUP_ROLLED_BACK')
        self.assertIsNone(self.manager.load_current())
        self.assert_no_execution()

    def test_pending_run_wrong_boot_or_unattributed_result_never_claims_success(self):
        self.manager.apply(self.manager.plan(self.config()))
        self.interrupt = True
        with self.assertRaises(KeyboardInterrupt): self.manager.run_once(self.manager.run_plan())
        self.active = 'active'; self.sub = 'exited'
        self.path('/proc/sys/kernel/random/boot_id').write_text('22222222-2222-2222-2222-222222222222\n')
        with self.assertRaises(RootActionError): self.manager.restore(self.manager.restore_plan())
        self.path('/proc/sys/kernel/random/boot_id').write_text('11111111-1111-1111-1111-111111111111\n')
        self.invocation = ''
        with self.assertRaises(RootActionError): self.manager.restore(self.manager.restore_plan())
        self.assertEqual(self.canary.read_text(), 'run\n')
        self.assertIsNotNone(self.manager.status()['pending'])

    def test_status_keeps_pending_visible_while_run_is_activating(self):
        self.manager.apply(self.manager.plan(self.config()))
        self.interrupt = True
        with self.assertRaises(KeyboardInterrupt): self.manager.run_once(self.manager.run_plan())
        self.active = 'activating'; self.sub = 'start'
        state = self.manager.status()
        self.assertIsNotNone(state['pending'])
        self.assertEqual(state['runtime']['active_state'], 'activating')
        with self.assertRaises(RootActionError): self.manager.restore_plan()

    def test_restore_blocks_external_edit_and_stale_preview(self):
        self.manager.apply(self.manager.plan(self.config(enabled=True)))
        preview = self.manager.restore_plan()
        self.path(SCRIPT).write_bytes(self.path(SCRIPT).read_bytes() + b'# external\n')
        with self.assertRaises(RootActionError): self.manager.restore(preview)
        self.assertTrue(self.enabled)
        self.assertIn(b'external', self.path(SCRIPT).read_bytes())

    def test_unit_blocks_loader_injection_before_env_program_starts(self):
        self.manager.apply(self.manager.plan(self.config()))
        unit = self.path(UNIT).read_text()
        self.assertIn('UnsetEnvironment=LD_PRELOAD LD_LIBRARY_PATH BASH_ENV ENV', unit)
        self.assertIn('UMask=0077', unit)

    def test_pending_id_remains_visible_when_external_file_blocks_recovery(self):
        self.manager.apply(self.manager.plan(self.config()))
        self.interrupt = True
        with self.assertRaises(KeyboardInterrupt): self.manager.run_once(self.manager.run_plan())
        self.path(SCRIPT).write_bytes(b'# external\n')
        state = self.manager.status()
        self.assertFalse(state['supported'])
        self.assertIsNotNone(state['pending'])
        self.assertEqual(state['pending']['kind'], 'startup-run')

    def test_successful_apply_can_still_restore_after_failed_apply_compensation(self):
        self.manager.apply(self.manager.plan(self.config()))
        self.fail_enable = True
        with self.assertRaises(RootActionError): self.manager.apply(self.manager.plan(self.config(enabled=True)))
        self.assertEqual(self.manager.restore(self.manager.restore_plan())['status'], 'STARTUP_RESTORED')
        self.assertIsNone(self.manager.load_current())

    def test_two_historical_restores_follow_configuration_not_replaced_inodes(self):
        first = self.config(enabled=False)
        self.manager.apply(self.manager.plan(first))
        self.manager.apply(self.manager.plan(self.config(script='#!/bin/bash\necho second\n', enabled=True)))
        self.manager.run_once(self.manager.run_plan())
        self.assertEqual(self.manager.restore(self.manager.restore_plan())['status'], 'STARTUP_RESTORED')
        self.assertEqual(self.manager.load_current(), first)
        self.assertEqual(self.manager.restore(self.manager.restore_plan())['status'], 'STARTUP_RESTORED')
        self.assertIsNone(self.manager.load_current())
        self.assertEqual(self.active, 'active')
        self.assertEqual(self.canary.read_text(), 'run\n')

    def test_restore_command_failure_compensates_configuration_and_boot_only(self):
        self.manager.apply(self.manager.plan(self.config()))
        self.manager.apply(self.manager.plan(self.config(script='#!/bin/bash\necho second\n', enabled=True)))
        before = {path: self.path(path).read_bytes() for path in (SCRIPT, UNIT, OWNED)}
        original = self.host.run
        failed = False
        def fail_reload(argv, timeout=30):
            nonlocal failed
            if argv[:2] == ['systemctl', 'daemon-reload'] and not failed:
                failed = True
                return subprocess.CompletedProcess(argv, 1, '', 'secret-native-error')
            return original(argv, timeout)
        with patch.object(self.host, 'run', side_effect=fail_reload):
            self.assertEqual(self.manager.restore(self.manager.restore_plan())['status'], 'STARTUP_ROLLED_BACK')
        self.assertEqual({path: self.path(path).read_bytes() for path in before}, before)
        self.assertTrue(self.enabled)
        self.assert_no_execution()

    def test_terminal_restore_journal_failure_keeps_latest_apply_restorable(self):
        self.manager.apply(self.manager.plan(self.config()))
        second = self.manager.apply(self.manager.plan(self.config(script='#!/bin/bash\necho second\n', enabled=True)))
        original = self.manager._root._journal
        failed = False
        def fail_commit(journal):
            nonlocal failed
            if journal['kind'] == 'startup-restore' and journal['status'] == 'STARTUP_RESTORED' and not failed:
                failed = True
                raise OSError('terminal journal failure')
            return original(journal)
        with patch.object(self.manager._root, '_journal', side_effect=fail_commit):
            result = self.manager.restore(self.manager.restore_plan())
        self.assertEqual(result['status'], 'STARTUP_ROLLED_BACK')
        self.assertIn('echo second', self.manager.load_current()['script'])
        self.assertEqual(self.manager.restore_plan()['transaction_id'], second['transaction_id'])
        self.assertEqual(self.manager.restore(self.manager.restore_plan())['status'], 'STARTUP_RESTORED')
        self.assertEqual(self.manager.load_current(), self.config())
        self.assert_no_execution()

    def test_crash_before_restore_commit_does_not_consume_source_apply(self):
        self.manager.apply(self.manager.plan(self.config()))
        second = self.manager.apply(self.manager.plan(self.config(script='#!/bin/bash\necho second\n', enabled=True)))
        original = self.manager._root._journal
        def crash_before_commit(journal):
            if journal['kind'] == 'startup-restore' and journal['status'] == 'STARTUP_RESTORED':
                raise KeyboardInterrupt()
            return original(journal)
        with patch.object(self.manager._root, '_journal', side_effect=crash_before_commit):
            with self.assertRaises(KeyboardInterrupt): self.manager.restore(self.manager.restore_plan())
        preview = self.manager.restore_plan()
        self.assertTrue(preview['recovery'])
        self.assertEqual(self.manager.restore(preview)['status'], 'STARTUP_ROLLED_BACK')
        self.assertIn('echo second', self.manager.load_current()['script'])
        self.assertEqual(self.manager.restore_plan()['transaction_id'], second['transaction_id'])
        self.assert_no_execution()

    def test_postcommit_error_then_compensation_crash_retains_pending_recovery(self):
        self.manager.apply(self.manager.plan(self.config()))
        second = self.manager.apply(self.manager.plan(self.config(script='#!/bin/bash\necho second\n', enabled=True)))
        original_journal = self.manager._root._journal
        original_write = self.manager._write_snapshot
        failed = False
        def write_then_fail(journal):
            nonlocal failed
            original_journal(journal)
            if journal['kind'] == 'startup-restore' and journal['status'] == 'STARTUP_RESTORED' and not failed:
                failed = True
                raise OSError('commit acknowledged late')
        def crash_compensation(snapshot, tombstone=None):
            if failed: raise KeyboardInterrupt()
            return original_write(snapshot, tombstone)
        with patch.object(self.manager._root, '_journal', side_effect=write_then_fail), patch.object(self.manager, '_write_snapshot', side_effect=crash_compensation):
            with self.assertRaises(KeyboardInterrupt): self.manager.restore(self.manager.restore_plan())
        self.assertIsNotNone(self.manager.status()['pending'])
        self.assertEqual(self.manager.restore(self.manager.restore_plan())['status'], 'STARTUP_ROLLED_BACK')
        self.assertEqual(self.manager.restore_plan()['transaction_id'], second['transaction_id'])
        self.assert_no_execution()

    def test_managed_symlink_and_metadata_changes_are_not_adopted(self):
        self.manager.apply(self.manager.plan(self.config()))
        preview = self.manager.plan(self.config(enabled=True))
        self.path(SCRIPT).chmod(0o400)
        try:
            with self.assertRaises(RootActionError): self.manager.apply(preview)
        finally:
            self.path(SCRIPT).chmod(0o700)
        target = self.root / 'external.sh'; target.write_text('# external')
        self.path(SCRIPT).unlink()
        try: self.path(SCRIPT).symlink_to(target)
        except OSError: self.skipTest('Native symlink privilege unavailable')
        with self.assertRaises(RootActionError): self.manager.plan(self.config())
        self.assertEqual(target.read_text(), '# external')


if __name__ == '__main__': unittest.main()

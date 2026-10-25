"""APT boundary tests never execute the native package manager."""
from contextlib import contextmanager
import copy
from pathlib import Path
import subprocess
import tempfile
import unittest

from server_setting.root_actions import Host, RootActionError, RootManager


class AptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'var/lib/apt/lists').mkdir(parents=True)
        (self.root / 'var/lib/dpkg').mkdir()
        (self.root / 'var/lib/dpkg/status').write_text('Package: demo\nVersion: 1\n')
        (self.root / 'var/lib/apt/lists/mirror_Packages').write_text('demo 2\n')
        self.calls, self.journals, self.logs = [], [], []
        self.failed, self.gated, self.log_failed = False, False, False
        outer = self

        class FakeHost(Host):
            def run(self, argv, timeout=30):
                outer.calls.append((list(argv), timeout))
                simulation = '--simulate' in argv
                output = ('The following packages have been kept back:\n  held-package\n'
                          'The following packages will be upgraded:\n  demo\n'
                          'Inst demo [1.0] (2.0 Ubuntu:26.04 [amd64])\n'
                          'Inst dependency (1.1 Ubuntu:26.04 [amd64])\n') if simulation else 'https://secret-source.invalid/raw-output'
                return subprocess.CompletedProcess(argv, int(outer.failed), output, 'private-url')

        class Manager:
            host = FakeHost(outer.root)
            def _gate(self, module):
                outer.gated = True
                if module != 'time':
                    raise AssertionError('wrong platform gate')
            @contextmanager
            def _lock(self):
                yield
            def _check_pending(self):
                pass
            def _preflight_state(self):
                pass
            def _id(self):
                return 'transaction'
            def _journal(self, value):
                outer.journals.append(copy.deepcopy(value))
            def _log(self, *value):
                if outer.log_failed:
                    raise RootActionError('Log unavailable')
                outer.logs.append(value)
            def _log_result(self, result):
                return RootManager._log_result(self, result)
        self.manager = Manager()

    def api(self):
        from server_setting.root_apt import plan, run
        return plan, run

    def test_upgrade_preview_is_structured_and_does_not_mutate(self):
        plan, _ = self.api()
        result = plan(self.manager, 'upgrade')
        self.assertTrue(self.gated)
        self.assertEqual(result['packages'], [{'name': 'demo', 'installed': '1.0', 'candidate': '2.0'},
                                               {'name': 'dependency', 'installed': None, 'candidate': '1.1'}])
        self.assertEqual(result['kept_back'], ['held-package'])
        self.assertEqual(self.journals, [])
        self.assertNotIn('secret-source', str(result))
        argv, timeout = self.calls[-1]
        self.assertIn('--with-new-pkgs', argv)
        self.assertIn('--no-remove', argv)
        self.assertIn('--simulate', argv)
        self.assertNotIn('--yes', argv)
        self.assertEqual(timeout, 900)

    def test_update_is_strict_and_records_sanitized_result(self):
        plan, run = self.api()
        preview = plan(self.manager, 'update')
        self.assertEqual(self.calls, [])
        result = run(self.manager, preview)
        self.assertEqual(self.calls[-1], (['apt-get', '--error-on=any', 'update'], 900))
        self.assertEqual(result['status'], 'APT_UPDATED')
        self.assertFalse(result['reboot_required'])
        self.assertNotIn('secret-source', str(result) + str(self.journals) + str(self.logs))

    def test_upgrade_preserves_local_conffiles_and_never_removes_or_reboots(self):
        plan, run = self.api()
        (self.root / 'run').mkdir()
        (self.root / 'run/reboot-required').touch()
        result = run(self.manager, plan(self.manager, 'upgrade'))
        argv, _ = self.calls[-1]
        self.assertIn('--yes', argv)
        self.assertIn('--no-remove', argv)
        self.assertIn('Dpkg::Options::=--force-confdef', argv)
        self.assertIn('Dpkg::Options::=--force-confold', argv)
        self.assertFalse(any(word in argv for word in ('autoremove', 'dist-upgrade', 'full-upgrade', 'reboot')))
        self.assertTrue(result['reboot_required'])
        self.assertEqual(self.journals[-1]['status'], 'APT_UPGRADED')

    def test_stale_or_tampered_preview_is_rejected_before_actual_apt(self):
        plan, run = self.api()
        preview = plan(self.manager, 'upgrade')
        (self.root / 'var/lib/dpkg/status').write_text('external change')
        with self.assertRaises(RootActionError) as error:
            run(self.manager, preview)
        self.assertEqual(error.exception.code, 'STALE_PLAN')
        self.assertTrue(all('--simulate' in argv for argv, _ in self.calls))
        self.assertEqual(self.journals, [])
        preview = plan(self.manager, 'update')
        preview['command'].append('dist-upgrade')
        with self.assertRaises(RootActionError):
            run(self.manager, preview)

    def test_failure_is_not_success_and_raw_apt_output_is_not_exposed(self):
        plan, run = self.api()
        preview = plan(self.manager, 'update')
        self.failed = True
        with self.assertRaises(RootActionError) as error:
            run(self.manager, preview)
        self.assertEqual(error.exception.code, 'APT_FAILED')
        self.assertEqual(self.journals[-1]['status'], 'APT_FAILED')
        self.assertNotIn('private-url', str(error.exception) + str(self.journals))

    def test_unknown_operation_never_executes(self):
        plan, _ = self.api()
        for operation in ('autoremove', 'dist-upgrade', 'update; reboot'):
            with self.assertRaises(RootActionError):
                plan(self.manager, operation)
        self.assertEqual(self.calls, [])

    def test_log_failure_preserves_apt_success_or_sanitized_failure(self):
        plan, run = self.api()
        self.log_failed = True
        result = run(self.manager, plan(self.manager, 'update'))
        self.assertEqual(result['status'], 'APT_UPDATED_WITH_LOG_WARNING')
        self.assertIn('warning', result)
        self.failed = True
        with self.assertRaises(RootActionError) as error:
            run(self.manager, plan(self.manager, 'update'))
        self.assertEqual(error.exception.code, 'APT_FAILED')

    def test_simulation_refuses_removal_and_retains_phased_packages(self):
        from server_setting.root_apt import _simulation
        with self.assertRaises(RootActionError):
            _simulation('Remv demo [1]\n')
        with self.assertRaises(RootActionError):
            _simulation('Inst invalid-unparsed-version\n')
        self.assertEqual(_simulation('The following upgrades have been deferred due to phasing:\n  phased-package\n')[1],
                         ['phased-package'])

    def test_simulation_refuses_unpreviewed_configure_only_packages(self):
        from server_setting.root_apt import _simulation
        with self.assertRaises(RootActionError) as error:
            _simulation('Conf pending-package (1.0 Ubuntu:26.04 [amd64])\n')
        self.assertEqual(error.exception.code, 'APT_UNSAFE_PLAN')
        packages, _ = _simulation('Inst demo [1.0] (2.0 Ubuntu:26.04 [amd64])\n'
                                  'Conf demo (2.0 Ubuntu:26.04 [amd64])\n')
        self.assertEqual(packages, [{'name': 'demo', 'installed': '1.0', 'candidate': '2.0'}])

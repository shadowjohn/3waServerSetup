"""Explicit native-module diagnostics preserve the current PHP configuration."""
from pathlib import Path
import os
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from server_setting import diagnostics


class SQLiteDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.module = Path(self.tmp.name) / 'mod_spatialite.so'
        self.module.write_bytes(b'trusted-test-native-library-not-executed')

    def call_with_mock_runner(self, acceptance=False):
        with patch('server_setting.diagnostics.resolve', return_value='/usr/bin/php'), \
             patch('server_setting.diagnostics.environment', return_value={'PATH': '/safe'}), \
             patch('server_setting.diagnostics._linux_root', return_value=False), \
             patch('server_setting.diagnostics.subprocess.run', return_value=SimpleNamespace(
                 returncode=0, stdout='PASS memory-only geometry query', stderr='')) as run:
            result = diagnostics.sqlite_spatial_smoke(str(self.module), acceptance=acceptance)
        return result, run

    def test_quick_module_runner_tests_sql_loader_and_safe_environment_explicitly(self):
        self.assertTrue(callable(getattr(diagnostics, 'sqlite_spatial_smoke', None)),
                        'SQLite UI needs an explicit trusted-module diagnostic entry')
        result, run = self.call_with_mock_runner()
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], '/usr/bin/php')
        self.assertIn('--module=' + str(self.module), argv)
        self.assertIn('--loader=legacy-sql', argv)
        self.assertNotIn('--acceptance', argv)
        self.assertIn('auto_prepend_file=', argv)
        self.assertIn('auto_append_file=', argv)
        self.assertFalse(any('feather_sql_load_extension=' in arg or 'extension_dir=' in arg for arg in argv))
        self.assertEqual(run.call_args.kwargs['env'], {'PATH': '/safe'})
        self.assertNotIn('shell', run.call_args.kwargs)
        self.assertIn('CLI ONLY', result)
        self.assertIn('FPM UNVERIFIED', result)
        self.assertIn('exit=0', result)

    def test_acceptance_selects_existing_unchanged_dual_driver_workload(self):
        self.assertTrue(callable(getattr(diagnostics, 'sqlite_spatial_smoke', None)))
        _, run = self.call_with_mock_runner(acceptance=True)
        argv = run.call_args.args[0]
        self.assertIn('--acceptance', argv)
        self.assertIn('--module=' + str(self.module), argv)
        self.assertNotIn('--loader=legacy-sql', argv)
        self.assertGreater(run.call_args.kwargs['timeout'], 20)

    def test_invalid_or_missing_module_never_reaches_native_execution(self):
        self.assertTrue(callable(getattr(diagnostics, 'sqlite_spatial_smoke', None)))
        with patch('server_setting.diagnostics.subprocess.run') as run:
            for value in (None, '', 'relative/mod_spatialite.so', 'https://host/mod_spatialite.so',
                          str(self.module) + '\x00', str(self.module.with_suffix('.php')),
                          str(Path(self.tmp.name) / 'missing.so')):
                with self.subTest(value=value):
                    result = diagnostics.sqlite_spatial_smoke(value)
                    self.assertTrue(result.startswith(('UNVERIFIED:', 'FAIL:')), result)
            run.assert_not_called()

    @unittest.skipIf(os.name == 'nt', 'Native POSIX ownership and symlinks')
    def test_root_refuses_writable_native_module_and_symlink_parent(self):
        self.assertTrue(callable(getattr(diagnostics, 'sqlite_spatial_smoke', None)))
        with patch('server_setting.diagnostics._linux_root', return_value=True), \
             patch('server_setting.diagnostics.subprocess.run') as run:
            self.module.chmod(0o666)
            self.assertTrue(diagnostics.sqlite_spatial_smoke(str(self.module)).startswith('FAIL:'))
            self.module.chmod(0o644)
            writable = Path(self.tmp.name) / 'writable'
            writable.mkdir(mode=0o777)
            writable.chmod(0o777)
            alias = writable / 'mod_spatialite.so'
            alias.symlink_to(self.module)
            self.assertTrue(diagnostics.sqlite_spatial_smoke(str(alias)).startswith('FAIL:'))
            run.assert_not_called()

    def test_root_owner_is_checked_for_every_resolved_and_alias_component(self):
        self.assertTrue(callable(getattr(diagnostics, 'sqlite_spatial_smoke', None)))
        original_lstat = Path.lstat
        def metadata(path, *args, **kwargs):
            result = original_lstat(path, *args, **kwargs)
            return SimpleNamespace(st_uid=1000 if path == self.module else 0,
                                   st_mode=(result.st_mode & ~0o022))
        with patch('server_setting.diagnostics._linux_root', return_value=True), \
             patch.object(Path, 'lstat', metadata), \
             patch('server_setting.diagnostics.subprocess.run') as run:
            self.assertTrue(diagnostics.sqlite_spatial_smoke(str(self.module)).startswith('FAIL:'))
            run.assert_not_called()

    def test_root_checks_intermediate_link_and_its_parent_before_native_execution(self):
        alias = Path(self.tmp.name) / 'alias' / 'mod_spatialite.so'
        middle = Path(self.tmp.name) / 'middle' / 'mod_spatialite.so'
        for path in (alias, middle):
            path.parent.mkdir()
            path.write_bytes(b'placeholder for a platform-independent link graph')
        original_lstat, original_resolve, original_readlink = Path.lstat, Path.resolve, Path.readlink
        for fault in ('middle-link-owner', 'middle-parent-writable', None):
            def metadata(path, *args, **kwargs):
                native = original_lstat(path, *args, **kwargs)
                mode = native.st_mode & ~0o022
                owner = 0
                if path in (alias, middle):
                    mode = stat.S_IFLNK | 0o777
                if path == middle and fault == 'middle-link-owner':
                    owner = 1000
                if path == middle.parent and fault == 'middle-parent-writable':
                    mode |= 0o022
                return SimpleNamespace(st_uid=owner, st_mode=mode)
            with self.subTest(fault=fault), \
                 patch('server_setting.diagnostics._linux_root', return_value=True), \
                 patch.object(Path, 'lstat', metadata), \
                 patch.object(Path, 'resolve', lambda path, **kw: self.module if path == alias else original_resolve(path, **kw)), \
                 patch.object(Path, 'readlink', lambda path: middle if path == alias else self.module if path == middle else original_readlink(path)), \
                 patch('server_setting.diagnostics.resolve', return_value='/usr/bin/php'), \
                 patch('server_setting.diagnostics.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='PASS', stderr='')) as run:
                result = diagnostics.sqlite_spatial_smoke(str(alias))
                if fault is None:
                    self.assertIn('exit=0', result)
                    self.assertIn('--module=' + str(self.module), run.call_args.args[0])
                    self.assertNotIn('--module=' + str(alias), run.call_args.args[0])
                else:
                    self.assertTrue(result.startswith('FAIL:'))
                    run.assert_not_called()

    def test_timeout_does_not_claim_passed_or_modify_php_settings(self):
        self.assertTrue(callable(getattr(diagnostics, 'sqlite_spatial_smoke', None)))
        with patch('server_setting.diagnostics.resolve', return_value='/usr/bin/php'), \
             patch('server_setting.diagnostics._linux_root', return_value=False), \
             patch('server_setting.diagnostics.subprocess.run', side_effect=subprocess.TimeoutExpired('php', 300)):
            result = diagnostics.sqlite_spatial_smoke(str(self.module), acceptance=True)
        self.assertIn('UNVERIFIED', result)
        self.assertIn('TimeoutExpired', result)


if __name__ == '__main__':
    unittest.main()

"""Command trust tests replace only executable lookup and OS metadata."""
import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

try:
    from server_setting import trusted_commands
except ImportError:
    trusted_commands = None


class TrustedCommandsTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(trusted_commands, 'Shared trusted command boundary is not implemented')

    def test_linux_resolution_ignores_inherited_path(self):
        with patch.object(trusted_commands.platform, 'system', return_value='Linux'), \
                patch.object(trusted_commands.os, 'geteuid', return_value=1000, create=True), \
                patch.object(trusted_commands.shutil, 'which', return_value='/usr/bin/git') as which:
            self.assertEqual(trusted_commands.resolve('git'), '/usr/bin/git')
        which.assert_called_once_with('git', path='/usr/sbin:/usr/bin:/sbin:/bin')

    def test_root_refuses_user_owned_or_writable_binary_and_parent(self):
        binary = Path('/usr/bin/git')
        for unsafe, owner, mode in [(binary, 1000, 0o755), (binary.parent, 1000, 0o755),
                                    (binary, 0, 0o775), (binary.parent, 0, 0o777)]:
            def metadata(path):
                return SimpleNamespace(st_uid=owner if path == unsafe else 0,
                                       st_mode=(stat.S_IFREG if path == binary else stat.S_IFDIR) | (mode if path == unsafe else 0o755))
            with self.subTest(unsafe=str(unsafe), owner=owner, mode=mode), \
                    patch.object(trusted_commands.platform, 'system', return_value='Linux'), \
                    patch.object(trusted_commands.os, 'geteuid', return_value=0, create=True), \
                    patch.object(trusted_commands.shutil, 'which', return_value='/usr/bin/git'), \
                    patch.object(Path, 'resolve', return_value=binary), \
                    patch.object(Path, 'stat', metadata):
                self.assertIsNone(trusted_commands.resolve('git'))

    def test_root_accepts_only_root_controlled_binary_and_ancestors(self):
        binary = Path('/usr/bin/git')
        def metadata(path):
            return SimpleNamespace(st_uid=0, st_mode=(stat.S_IFREG if path == binary else stat.S_IFDIR) | 0o755)
        with patch.object(trusted_commands.platform, 'system', return_value='Linux'), \
                patch.object(trusted_commands.os, 'geteuid', return_value=0, create=True), \
                patch.object(trusted_commands.shutil, 'which', return_value='/usr/bin/git'), \
                patch.object(Path, 'resolve', return_value=binary), \
                patch.object(Path, 'stat', metadata):
            self.assertEqual(trusted_commands.resolve('git'), '/usr/bin/git')

    def test_root_environment_does_not_inherit_interpreter_loader_or_git_controls(self):
        unsafe = {'PATH': '/untrusted', 'PHPRC': '/untrusted/php.ini', 'PHP_INI_SCAN_DIR': '/untrusted',
                  'LD_PRELOAD': '/untrusted/lib.so', 'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.pager',
                  'GIT_CONFIG_VALUE_0': 'untrusted', 'GIT_EXEC_PATH': '/untrusted'}
        with patch.object(trusted_commands.platform, 'system', return_value='Linux'), \
                patch.object(trusted_commands.os, 'geteuid', return_value=0, create=True), \
                patch.dict(os.environ, unsafe, clear=True):
            env = trusted_commands.environment()
        self.assertEqual(env['PATH'], '/usr/sbin:/usr/bin:/sbin:/bin')
        self.assertEqual(env['LC_ALL'], 'C')
        self.assertEqual(env['HOME'], '/root')
        for key in set(unsafe) - {'PATH'}:
            self.assertNotIn(key, env)

    def test_windows_readonly_resolution_and_environment_remain_compatible(self):
        with patch.object(trusted_commands.platform, 'system', return_value='Windows'), \
                patch.object(trusted_commands.shutil, 'which', return_value='C:/Tools/php.exe') as which, \
                patch.dict(os.environ, {'PATH': 'C:/Tools', 'CUSTOM_READONLY': 'kept'}, clear=True):
            self.assertEqual(trusted_commands.resolve('php'), 'C:/Tools/php.exe')
            self.assertEqual(trusted_commands.environment()['CUSTOM_READONLY'], 'kept')
        which.assert_called_once_with('php')

    def test_explicit_paths_and_shell_strings_are_not_resolved(self):
        for command in ('/tmp/git', '../git', 'git --version', 'cmd\\git', ''):
            with self.subTest(command=command):
                self.assertIsNone(trusted_commands.resolve(command))

    def test_revision_uses_resolved_executable_and_safe_environment(self):
        from server_setting import revision
        result = subprocess.CompletedProcess([], 0, 'aabbcc112233\n', '')
        with patch('server_setting.trusted_commands.resolve', return_value='/usr/bin/git'), \
                patch('server_setting.trusted_commands.environment', return_value={'PATH': '/safe'}), \
                patch('subprocess.run', return_value=result) as run:
            self.assertEqual(revision(), 'aabbcc112233')
        self.assertEqual(run.call_args.args[0][0], '/usr/bin/git')
        self.assertEqual(run.call_args.kwargs['env'], {'PATH': '/safe'})

    def test_revision_missing_trusted_git_does_not_execute(self):
        from server_setting import revision
        with patch('server_setting.trusted_commands.resolve', return_value=None), patch('subprocess.run') as run:
            self.assertEqual(revision(), 'unknown')
        run.assert_not_called()

    def test_sqlite_smoke_uses_resolved_executable_and_safe_environment(self):
        from server_setting.diagnostics import sqlite_smoke
        result = subprocess.CompletedProcess([], 0, 'PASS fixture', '')
        with patch('server_setting.diagnostics.resolve', return_value='/usr/bin/php'), \
                patch('server_setting.diagnostics.environment', return_value={'PATH': '/safe'}), \
                patch('subprocess.run', return_value=result) as run:
            self.assertIn('exit=0', sqlite_smoke())
        self.assertEqual(run.call_args.args[0][0], '/usr/bin/php')
        self.assertEqual(run.call_args.kwargs['env'], {'PATH': '/safe'})


if __name__ == '__main__':
    unittest.main()

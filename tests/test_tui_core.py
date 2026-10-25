import unittest
from unittest.mock import patch


class CoreTests(unittest.TestCase):
    def test_windows_is_unavailable_not_pass(self):
        from server_setting.probes import collect
        snapshot = collect(system='Windows')
        self.assertEqual(snapshot['platform'], 'Windows')
        for item in snapshot['modules'].values():
            self.assertEqual(item['status'], 'UNAVAILABLE')
            self.assertNotIn('PASS', item['detail'])

    def test_preview_reports_unknown_and_no_system_apply(self):
        from server_setting.model import preview
        output = preview('php', {'memory_limit': '256M'}, {})
        self.assertIn('UNKNOWN', output)
        self.assertIn('256M', output)
        self.assertIn('PREVIEW ONLY', output)
        self.assertNotIn('PASS', output)

    def test_unchanged_known_value_is_no_difference_not_health(self):
        from server_setting.model import preview
        self.assertIn('NO DIFFERENCE', preview('php', {'memory_limit': '128M'}, {'memory_limit': '128M'}))

    def test_php_parser_only_returns_managed_keys(self):
        from server_setting.probes import parse_values
        text = ';memory_limit=1M\nmemory_limit=128M\npassword=SECRET\n[PHP]\nshort_open_tag = On\nmemory_limit=256M\n'
        values = parse_values(text, {'memory_limit', 'short_open_tag'})
        self.assertEqual(values, {'memory_limit': '256M', 'short_open_tag': 'On'})

    def test_bad_inputs_are_preview_errors(self):
        from server_setting.model import validate
        for field, value in [('memory_limit', 'bad'), ('session.upload_progress.enabled', 'maybe'),
                             ('upload_max_filesize', '-1'), ('date.timezone', '../oops')]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate(field, value)

    def test_offline_integrations_are_candidates(self):
        from server_setting.model import firewall_preview, apache_candidate
        text = firewall_preview()
        self.assertIn('OFFLINE TEXT ONLY', text)
        self.assertNotIn('--dport 80 ', text)
        self.assertIn('Require all denied', apache_candidate())

    def test_mutating_commands_never_reach_runner(self):
        from server_setting.probes import _command
        with patch('server_setting.probes.subprocess.run') as run:
            for argv in [['systemctl', 'restart', 'apache2'], ['timedatectl', 'set-timezone', 'Asia/Taipei'],
                         ['findmnt', '--help'], ['ssh', 'host']]:
                self.assertIsNone(_command(argv))
            run.assert_not_called()

    def test_linux_observations_are_not_runtime_health(self):
        from server_setting.probes import collect
        def read(path):
            return {'/etc/os-release': 'ID=ubuntu\nVERSION_ID="26.04"',
                    '/proc/swaps': 'Filename Type Size Used Priority\n'}.get(str(path))
        def command(argv):
            if argv[0] == 'systemctl':
                return 'LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n'
            return None
        with patch('server_setting.probes._read', side_effect=read), patch('server_setting.probes._command', side_effect=command), patch('server_setting.probes.Path.glob', return_value=[]):
            snapshot = collect(system='Linux')
        self.assertTrue(snapshot['supported_target'])
        self.assertEqual(snapshot['modules']['apache']['status'], 'OBSERVED')
        self.assertEqual(snapshot['modules']['apache']['runtime'], 'UNVERIFIED')
        self.assertEqual(snapshot['modules']['time']['status'], 'UNKNOWN')
        self.assertEqual(snapshot['modules']['storage']['status'], 'OBSERVED')

    def test_pasv_and_upload_relationships(self):
        from server_setting.model import preview
        with self.assertRaises(ValueError):
            preview('vsftpd', {'pasv_min_port': '60100', 'pasv_max_port': '60000'}, {})
        with self.assertRaises(ValueError):
            preview('php', {'upload_max_filesize': '256M', 'post_max_size': '128M'}, {})

    def test_sqlite_explicit_runner_disables_startup_scripts(self):
        from server_setting.diagnostics import sqlite_smoke
        from types import SimpleNamespace
        with patch('server_setting.diagnostics.resolve', return_value='/local/php'), patch('server_setting.diagnostics.subprocess.run', return_value=SimpleNamespace(returncode=2, stdout='UNVERIFIED', stderr='')) as run:
            output = sqlite_smoke()
        argv = run.call_args.args[0]
        self.assertIn('auto_prepend_file=', argv)
        self.assertIn('auto_append_file=', argv)
        self.assertNotIn('--module', argv)
        self.assertIn('exit=2', output)


if __name__ == '__main__':
    unittest.main()

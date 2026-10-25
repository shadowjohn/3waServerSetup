import contextlib
import io
import json
import unittest
from unittest.mock import patch


class MemoryManager:
    def __init__(self):
        self.changed = False
        self.saved = None

    def plan(self, module, values=None, service_active=None, service_enabled=None):
        return {'schema': 1, 'module': module, 'values': values or {'ServerTokens': 'Prod', 'ServerSignature': 'Off'},
                'service_active': service_active, 'service_enabled': service_enabled,
                'files': [], 'validate': [], 'apply_commands': [], 'fingerprint': 'trusted-plan'}

    def apply(self, plan):
        self.changed = True
        self.saved = plan
        return {'status': 'APPLIED', 'module': plan['module'], 'message': 'Saved',
                'verification': {'verified': False, 'health_scope': 'configuration only'}}

    def install(self, module):
        self.changed = True
        return {'status': 'INSTALLED', 'module': module, 'message': 'Selected packages installed'}

    def restore_last(self, module):
        self.changed = True
        return {'status': 'RESTORED', 'module': module, 'message': 'Previous files restored'}

    def apt_plan(self, operation):
        return {'module': 'apt', 'operation': operation, 'packages': [], 'kept_back': [], 'fingerprint': 'apt-preview'}

    def apt_run(self, plan):
        self.changed = True
        return {'module': 'apt', 'status': 'APT_UPDATED' if plan['operation'] == 'update' else 'APT_UPGRADED',
                'reboot_required': False}

    def mariadb_security_plan(self, remote_enabled=None, change_password=False):
        return {'module': 'mariadb-security', 'remote_enabled': remote_enabled,
                'change_password': change_password, 'fingerprint': 'security-preview', 'notes': []}

    def mariadb_security_apply(self, plan, password=None):
        self.changed = True
        self.security_received = (plan, password)
        return {'status': 'APPLIED', 'module': 'mariadb-security'}


class RootCliTests(unittest.TestCase):
    def invoke(self, arguments, root=True, manager=None):
        from server_setting.__main__ import main
        stream = io.StringIO()
        with patch('server_setting.__main__.root_management_available', return_value=root), contextlib.redirect_stdout(stream):
            code = main(arguments, root_manager=manager or MemoryManager())
        return code, json.loads(stream.getvalue())

    def test_all_root_commands_deny_nonroot_without_mutation(self):
        for option in ('--root-plan', '--root-apply', '--root-install', '--root-restore'):
            with self.subTest(option=option):
                manager = MemoryManager()
                code, result = self.invoke([option, 'apache', '--confirm'], root=False, manager=manager)
                self.assertNotEqual(code, 0)
                self.assertEqual(result['status'], 'DENIED')
                self.assertFalse(manager.changed)

    def test_mutations_require_confirmation(self):
        for option in ('--root-apply', '--root-install', '--root-restore'):
            with self.subTest(option=option):
                manager = MemoryManager()
                code, result = self.invoke([option, 'apache'], manager=manager)
                self.assertNotEqual(code, 0)
                self.assertEqual(result['code'], 'CONFIRMATION_REQUIRED')
                self.assertFalse(manager.changed)

    def test_apt_commands_require_root_and_explicit_confirmation(self):
        for option in ('--apt-update', '--apt-upgrade'):
            for arguments, root, status in [([option], True, 'ERROR'),
                                             ([option, '--confirm'], False, 'DENIED')]:
                manager = MemoryManager()
                code, result = self.invoke(arguments, root=root, manager=manager)
                self.assertNotEqual(code, 0)
                self.assertEqual(result['status'], status)
                self.assertFalse(manager.changed)
            manager = MemoryManager()
            code, result = self.invoke([option, '--confirm'], manager=manager)
            self.assertEqual(code, 0)
            self.assertTrue(manager.changed)
            self.assertIn(result['status'], {'APT_UPDATED', 'APT_UPGRADED'})
        manager = MemoryManager()
        code, result = self.invoke(['--apt-plan', 'upgrade'], manager=manager)
        self.assertEqual(code, 0)
        self.assertEqual(result['plan']['operation'], 'upgrade')
        self.assertFalse(manager.changed)
        code, result = self.invoke(['--apt-update', '--start', '--confirm'])
        self.assertEqual(code, 2)
        self.assertEqual(result['status'], 'ERROR')

    def test_mariadb_security_plan_never_reads_a_password(self):
        with patch('server_setting.__main__.getpass.getpass', side_effect=AssertionError('Password prompt during plan')):
            code, result = self.invoke(['--mariadb-security-plan', '--remote', 'on', '--change-root-password'])
        self.assertEqual(code, 0)
        self.assertIs(result['plan']['remote_enabled'], True)
        self.assertIs(result['plan']['change_password'], True)
        self.assertNotIn('password', {key for key in result['plan'] if key != 'change_password'})

    def test_mariadb_security_blocks_nonroot_unconfirmed_and_echo_fallback(self):
        for arguments, root in [(['--mariadb-security-apply', '--remote', 'off', '--confirm'], False),
                                 (['--mariadb-security-apply', '--remote', 'off'], True),
                                 (['--mariadb-security-apply', '--change-root-password', '--confirm'], True)]:
            manager = MemoryManager()
            with patch('sys.stdin', io.StringIO('never-read-secret\n')), \
                    patch('server_setting.__main__.getpass.getpass', side_effect=AssertionError('Insecure password prompt')):
                code, result = self.invoke(arguments, root=root, manager=manager)
            self.assertNotEqual(code, 0)
            self.assertFalse(manager.changed)
            self.assertNotIn('never-read-secret', str(result))

    def test_mariadb_security_explicit_stdin_is_bounded_and_not_echoed(self):
        secret = 'stdin-only-not-in-json'
        manager = MemoryManager()
        with patch('sys.stdin', io.StringIO(secret + '\n')):
            code, result = self.invoke(['--mariadb-security-apply', '--remote', 'on', '--change-root-password',
                                       '--password-stdin', '--confirm'], manager=manager)
        self.assertEqual(code, 0)
        self.assertEqual(manager.security_received[1], secret)
        self.assertNotIn(secret, str(result) + str(manager.security_received[0]))
        for invalid in ('x' * 4097 + '\n', '\n', 'bad\x00value\n'):
            with patch('sys.stdin', io.StringIO(invalid)):
                manager = MemoryManager()
                code, _ = self.invoke(['--mariadb-security-apply', '--change-root-password',
                                       '--password-stdin', '--confirm'], manager=manager)
            self.assertNotEqual(code, 0)
            self.assertFalse(manager.changed)

    def test_mariadb_security_tty_confirm_and_errors_never_include_password(self):
        from unittest.mock import MagicMock
        terminal = MagicMock()
        terminal.isatty.return_value = True
        manager = MemoryManager()
        secret = 'tty-only-no-output'
        with patch('sys.stdin', terminal), patch('server_setting.__main__.getpass.getpass', side_effect=[secret, secret]):
            code, result = self.invoke(['--mariadb-security-apply', '--change-root-password', '--confirm'], manager=manager)
        self.assertEqual(code, 0)
        self.assertEqual(manager.security_received[1], secret)
        self.assertNotIn(secret, str(result))
        with patch('sys.stdin', terminal), patch('server_setting.__main__.getpass.getpass', side_effect=[secret, 'mismatch']):
            manager = MemoryManager()
            code, result = self.invoke(['--mariadb-security-apply', '--change-root-password', '--confirm'], manager=manager)
        self.assertNotEqual(code, 0)
        self.assertFalse(manager.changed)
        self.assertNotIn(secret, str(result))
        manager = MemoryManager()
        with patch('sys.stdin', io.StringIO(secret + '\n')), \
                patch.object(manager, 'mariadb_security_apply', side_effect=RuntimeError(secret)):
            code, result = self.invoke(['--mariadb-security-apply', '--change-root-password', '--password-stdin', '--confirm'], manager=manager)
        self.assertNotEqual(code, 0)
        self.assertNotIn(secret, str(result))

    def test_mariadb_security_rejects_tty_stdin_echo_and_preserves_safe_error_code(self):
        from server_setting.root_actions import RootActionError
        terminal = io.StringIO('must-not-read-on-tty\n')
        terminal.isatty = lambda: True
        manager = MemoryManager()
        with patch('sys.stdin', terminal):
            code, result = self.invoke(['--mariadb-security-apply', '--change-root-password', '--password-stdin', '--confirm'], manager=manager)
        self.assertNotEqual(code, 0)
        self.assertFalse(manager.changed)
        with patch.object(manager, 'mariadb_security_plan', side_effect=RootActionError('private-error-text', 'MARIADB_NOT_READY')):
            code, result = self.invoke(['--mariadb-security-plan'], manager=manager)
        self.assertEqual(result['code'], 'MARIADB_NOT_READY')
        self.assertNotIn('private-error-text', str(result))

    def test_plan_keeps_active_and_boot_targets_separate(self):
        code, result = self.invoke(['--root-plan', 'apache', '--stop', '--enable'])
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'PLANNED')
        self.assertIs(result['plan']['service_active'], False)
        self.assertIs(result['plan']['service_enabled'], True)

    def test_custom_apply_reports_exact_verification_scope(self):
        manager = MemoryManager()
        values = {'ServerTokens': 'Prod', 'ServerSignature': 'Off'}
        code, result = self.invoke(['--root-apply', 'apache', '--values', json.dumps(values),
                                    '--start', '--disable', '--confirm'], manager=manager)
        self.assertEqual(code, 0)
        self.assertEqual(manager.saved['values'], values)
        self.assertIs(manager.saved['service_active'], True)
        self.assertIs(manager.saved['service_enabled'], False)
        self.assertEqual(result['status'], 'APPLIED')
        self.assertFalse(result['verification']['verified'])

    def test_install_and_restore_are_explicit_json_operations(self):
        for option, status in [('--root-install', 'INSTALLED'), ('--root-restore', 'RESTORED')]:
            with self.subTest(option=option):
                code, result = self.invoke([option, 'apache', '--confirm'])
                self.assertEqual(code, 0)
                self.assertEqual(result['status'], status)

    def test_invalid_json_module_and_conflicting_targets_are_json_errors(self):
        for arguments in [
            ['--root-plan', 'apache', '--values', '[1]'],
            ['--root-plan', 'apache', '--values', '{bad'],
            ['--root-plan', 'firewall'],
            ['--root-plan', 'apache', '--start', '--stop'],
            ['--root-install', 'apache', '--values', '{}', '--confirm'],
        ]:
            with self.subTest(arguments=arguments):
                manager = MemoryManager()
                code, result = self.invoke(arguments, manager=manager)
                self.assertNotEqual(code, 0)
                self.assertEqual(result['status'], 'ERROR')
                self.assertFalse(manager.changed)


class TimeProbeTests(unittest.TestCase):
    def test_untrusted_probe_executable_is_not_run(self):
        from types import SimpleNamespace
        from server_setting.probes import _command
        with patch('server_setting.probes.resolve', return_value=None), \
                patch.dict('os.environ', {'PATH': '/home/john/bin'}), \
                patch('server_setting.probes.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='UNTRUSTED')):
            self.assertIsNone(_command(['systemctl', 'show', 'apache2.service', '--no-pager',
                                        '--property=LoadState,ActiveState,UnitFileState']))

    def test_time_observation_requests_both_properties_separately(self):
        from types import SimpleNamespace
        from server_setting.probes import collect

        def run(argv, **kwargs):
            if argv[0].endswith('/timedatectl'):
                self.assertEqual(argv, ['/usr/bin/timedatectl', 'show', '--property=Timezone', '--property=NTPSynchronized'])
                text = 'Timezone=Asia/Taipei\nNTPSynchronized=yes\n'
            elif argv[0].endswith('/systemctl'):
                text = 'LoadState=not-found\n'
            else:
                text = 'ext4 20G 10G\n'
            return SimpleNamespace(returncode=0, stdout=text)

        def read(path):
            return {'/etc/os-release': 'ID=ubuntu\nVERSION_ID=26.04\n',
                    '/proc/swaps': 'Filename Type Size Used Priority\n'}.get(str(path))

        with patch('server_setting.probes._read', side_effect=read), \
                patch('server_setting.probes.Path.glob', return_value=[]), \
                patch('server_setting.probes.resolve', side_effect=lambda name: '/usr/bin/' + name, create=True), \
                patch('server_setting.probes.subprocess.run', side_effect=run):
            snapshot = collect(system='Linux')
        self.assertEqual(snapshot['modules']['time']['configured'],
                         {'Timezone': 'Asia/Taipei', 'NTPSynchronized': 'yes'})

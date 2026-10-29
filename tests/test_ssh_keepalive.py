"""Native-layout SSH keepalive transactions never stop or restart SSH."""
from contextlib import nullcontext, redirect_stdout
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from server_setting.model import FIELDS, validate
from server_setting.root_actions import Host, RootActionError, RootManager
from server_setting import root_recipes


TARGET = '/etc/ssh/sshd_config.d/00-3wa-server-setup.conf'


class SSHKeepaliveTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.calls = []
        self.reload_values = []
        self.active = True
        self.canonical_transport = False
        self.fail_reload = False
        self.fail_syntax = False
        self.unit_overrides = {}
        self.manager_environment = ''
        outer = self

        class SSHHost(Host):
            def run(self, argv, timeout=30):
                outer.calls.append(list(argv))
                code, output = 0, ''
                if argv[0] == 'dpkg-query':
                    output = 'install ok installed\n'
                elif argv[:2] == ['systemctl', 'show-environment']:
                    output = outer.manager_environment
                elif argv[:2] == ['systemctl', 'show'] and '--property=ExecStart' in argv:
                    properties = {'DropInPaths': '', 'EnvironmentFiles': '/etc/default/ssh (ignore_errors=yes)',
                                  'Environment': '', 'PassEnvironment': '', 'RootDirectory': '', 'RootImage': '',
                                  'ExecStart': '{ path=/usr/sbin/sshd ; argv[]=/usr/sbin/sshd -D $SSHD_OPTS ; }',
                                  'ExecReload': '{ path=/usr/sbin/sshd ; argv[]=/usr/sbin/sshd -t ; } ; { path=/bin/kill ; argv[]=/bin/kill -HUP $MAINPID ; }'}
                    properties.update(outer.unit_overrides)
                    output = ''.join(key + '=' + value + '\n' for key, value in properties.items()
                                     if key != 'EnvironmentFiles' or value)
                elif argv[:2] == ['systemctl', 'show']:
                    output = ('LoadState=loaded\nActiveState=' + ('active' if outer.active else 'inactive')
                              + '\nUnitFileState=enabled\n')
                elif argv == ['sshd', '-T']:
                    values = outer.effective()
                    if outer.canonical_transport and values['tcpkeepalive'] == 'yes':
                        values['tcpkeepalive'] = 'transport'
                    output = ''.join(key + ' ' + value + '\n' for key, value in values.items())
                    output += 'port 2222\npermitrootlogin prohibit-password\npasswordauthentication no\n'
                elif argv == ['sshd', '-t']:
                    if outer.fail_syntax and self.path(TARGET).exists():
                        code = 1
                elif argv == ['systemctl', 'reload', 'ssh.service']:
                    outer.reload_values.append(outer.effective())
                    if outer.fail_reload:
                        outer.fail_reload = False
                        code = 1
                else:
                    raise AssertionError('Unexpected SSH recipe command: ' + repr(argv))
                return subprocess.CompletedProcess(argv, code, output, 'PRIVATE_NATIVE_ERROR' if code else '')

        self.host = SSHHost(self.root)
        self.manager = RootManager(self.host)
        self.put('/etc/os-release', 'ID=ubuntu\nVERSION_ID="26.04"\n')
        self.put('/etc/ssh/sshd_config', '# Native server\nInclude /etc/ssh/sshd_config.d/*.conf\n'
                 'Port 2222\nPermitRootLogin prohibit-password\nPasswordAuthentication no\n'
                 'Match User backup\n    ClientAliveInterval 300\n')
        self.put('/etc/default/ssh', '# Leave daemon options unchanged\nSSHD_OPTS=\n')
        self.put('/etc/ssh/sshd_config.d/50-cloud-init.conf', 'PasswordAuthentication no\n')
        self.put('/root/.ssh/authorized_keys', 'existing public key\n')
        for patcher in (patch.object(self.manager, '_gate'),
                        patch.object(self.manager, '_lock', return_value=nullcontext())):
            patcher.start()
            self.addCleanup(patcher.stop)

    def put(self, virtual, text):
        path = self.host.path(virtual)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8', newline='')

    def effective(self):
        found = {}
        def parse(text):
            for line in text.splitlines():
                parts = shlex.split(line, comments=True)
                if not parts:
                    continue
                key = parts[0].lower()
                if key == 'match':
                    break
                if key == 'include':
                    for file in sorted(self.host.path('/etc/ssh/sshd_config.d').glob('*.conf')):
                        parse(file.read_text())
                elif key in {'clientaliveinterval', 'tcpkeepalive'}:
                    found.setdefault(key, parts[1])
        parse(self.host.read('/etc/ssh/sshd_config'))
        return {'clientaliveinterval': found.get('clientaliveinterval', '0'),
                'tcpkeepalive': found.get('tcpkeepalive', 'yes')}

    def recommended(self):
        self.assertEqual(FIELDS.get('ssh'), {'ClientAliveInterval': '60', 'TCPKeepAlive': 'yes'})
        return dict(FIELDS['ssh'])

    def test_defaults_and_custom_values_have_narrow_validation(self):
        self.recommended()
        for value in ('0', '60', '3600'):
            validate('ClientAliveInterval', value)
        for value in ('-1', '1m', '1.5', '60\nPermitRootLogin yes', '999999999999999'):
            with self.subTest(interval=value), self.assertRaises(ValueError):
                validate('ClientAliveInterval', value)
        for value in ('yes', 'no'):
            validate('TCPKeepAlive', value)
        for value in ('YES', '1', 'all', 'yes #comment'):
            with self.subTest(keepalive=value), self.assertRaises(ValueError):
                validate('TCPKeepAlive', value)

    def test_plan_is_readonly_and_never_declares_service_controls(self):
        plan = self.manager.plan('ssh', self.recommended())
        self.assertFalse(self.host.path(TARGET).exists())
        self.assertFalse(self.host.path('/var').exists())
        self.assertEqual([entry['path'] for entry in plan['files']], [TARGET])
        self.assertIsNone(plan['service_snapshot']['unit'])
        self.assertEqual(plan['validate'], [['sshd', '-t']])
        self.assertEqual(plan['apply_commands'], [['systemctl', 'reload', 'ssh.service']])
        state = root_recipes.capabilities(self.host, 'ssh')
        self.assertIsNone(state['unit'])
        self.assertTrue(state['ssh_service']['active'])
        for targets in ({'service_active': True}, {'service_active': False}, {'service_enabled': True}):
            with self.subTest(targets=targets), self.assertRaises((ValueError, RootActionError)):
                self.manager.plan('ssh', **targets)

    def test_apply_saves_two_global_settings_and_preserves_auth_keys_port_and_match(self):
        main = self.host.path('/etc/ssh/sshd_config').read_bytes()
        keys = self.host.path('/root/.ssh/authorized_keys').read_bytes()
        self.canonical_transport = True
        result = self.manager.apply(self.manager.plan('ssh', self.recommended()))
        self.assertEqual(result['status'], 'APPLIED')
        self.assertTrue(result['verification']['verified'])
        self.assertIn('global', result['verification']['detail'].lower())
        self.assertIn('Match', result['verification']['detail'])
        self.assertEqual(self.manager.load_saved('ssh')['values'], FIELDS['ssh'])
        self.assertEqual(self.host.path('/etc/ssh/sshd_config').read_bytes(), main)
        self.assertEqual(self.host.path('/root/.ssh/authorized_keys').read_bytes(), keys)
        self.assertEqual(self.host.read(TARGET).splitlines()[1:], ['ClientAliveInterval 60', 'TCPKeepAlive yes'])
        reload_at = self.calls.index(['systemctl', 'reload', 'ssh.service'])
        self.assertIn(['sshd', '-t'], self.calls[:reload_at])
        self.assertIn(['sshd', '-T'], self.calls[:reload_at])
        self.assertFalse(any(call[:2] == ['systemctl', verb]
                             for call in self.calls for verb in ('restart', 'stop', 'start', 'enable', 'disable')))

    def test_noop_does_not_reload_and_restore_deletes_only_the_created_dropin(self):
        self.recommended()
        self.manager.apply(self.manager.plan('ssh'))
        self.calls.clear()
        self.assertEqual(self.manager.apply(self.manager.plan('ssh'))['status'], 'NO_CHANGE')
        self.assertNotIn(['systemctl', 'reload', 'ssh.service'], self.calls)
        self.assertEqual(self.manager.restore_last('ssh')['status'], 'RESTORED')
        self.assertFalse(self.host.path(TARGET).exists())
        self.assertEqual(self.reload_values[-1]['clientaliveinterval'], '0')

    def test_inactive_ssh_is_configured_without_start_reload_or_boot_changes(self):
        self.recommended()
        self.active = False
        result = self.manager.apply(self.manager.plan('ssh'))
        self.assertEqual(result['status'], 'APPLIED')
        self.assertFalse(any(call[0] == 'systemctl' and call[1] not in {'show', 'show-environment'} for call in self.calls))
        self.manager.restore_last('ssh')
        self.assertFalse(any(call[0] == 'systemctl' and call[1] not in {'show', 'show-environment'} for call in self.calls))

    def test_first_obtained_conflict_is_refused_before_reloading_candidate(self):
        self.recommended()
        self.put('/etc/ssh/sshd_config', 'ClientAliveInterval 90\nInclude /etc/ssh/sshd_config.d/*.conf\n')
        with self.assertRaises(RootActionError) as raised:
            self.manager.apply(self.manager.plan('ssh'))
        self.assertIn('SSH global ClientAliveInterval', str(raised.exception))
        self.assertIn('/etc/ssh/sshd_config', str(raised.exception))
        self.assertFalse(self.host.path(TARGET).exists())
        self.assertTrue(all(values['clientaliveinterval'] == '90' for values in self.reload_values))

    def test_syntax_and_reload_failures_restore_original_without_restart(self):
        self.recommended()
        for failure in ('fail_syntax', 'fail_reload'):
            with self.subTest(failure=failure):
                setattr(self, failure, True)
                with self.assertRaises(RootActionError) as raised:
                    self.manager.apply(self.manager.plan('ssh'))
                self.assertNotIn('PRIVATE_NATIVE_ERROR', str(raised.exception))
                self.assertFalse(self.host.path(TARGET).exists())
                self.assertTrue(self.active)
                setattr(self, failure, False)
        self.assertFalse(any(call[:2] in (['systemctl', 'stop'], ['systemctl', 'restart']) for call in self.calls))

    def test_missing_global_include_or_include_in_match_is_refused(self):
        self.recommended()
        for main in ('Port 2222\n', 'Match User backup\nInclude /etc/ssh/sshd_config.d/*.conf\n'):
            self.put('/etc/ssh/sshd_config', main)
            with self.subTest(main=main), self.assertRaises((ValueError, RootActionError)):
                self.manager.plan('ssh')
        self.assertFalse(self.host.path(TARGET).exists())

    def test_nested_include_and_earlier_match_need_separate_integration(self):
        self.recommended()
        self.put('/etc/ssh/sshd_config.d/99-custom.conf', 'Include /srv/custom.conf\n')
        with self.assertRaises((ValueError, RootActionError)):
            self.manager.plan('ssh')
        self.host.path('/etc/ssh/sshd_config.d/99-custom.conf').unlink()
        self.put('/etc/ssh/sshd_config.d/00-0-site.conf', 'Match User backup\nClientAliveInterval 90\n')
        with self.assertRaises((ValueError, RootActionError)):
            self.manager.plan('ssh')

    def test_equals_delimited_directives_cannot_hide_include_or_match_scope(self):
        self.recommended()
        self.put('/etc/ssh/sshd_config.d/99-custom.conf', 'Include=/srv/custom.conf\n')
        with self.assertRaises((ValueError, RootActionError)):
            self.manager.plan('ssh')
        self.host.path('/etc/ssh/sshd_config.d/99-custom.conf').unlink()
        self.put('/etc/ssh/sshd_config', 'Match=User backup\nInclude /etc/ssh/sshd_config.d/*.conf\n')
        with self.assertRaises((ValueError, RootActionError)):
            self.manager.plan('ssh')

    def test_native_unit_printer_variants_are_supported_but_custom_exec_and_env_are_refused(self):
        self.recommended()
        self.unit_overrides = {'EnvironmentFiles': '',
                               'ExecStart': '{ path=/usr/sbin/sshd ; argv[]=/usr/sbin/sshd -D ; }'}
        self.assertTrue(self.manager.plan('ssh'))
        for overrides in ({'ExecStart': '{ path=/usr/sbin/sshd ; argv[]=/usr/sbin/sshd -D -f /private/SENSITIVE.conf ; }'},
                          {'DropInPaths': '/private/SENSITIVE.conf'},
                          {'EnvironmentFiles': '/private/SENSITIVE.conf (ignore_errors=yes)'},
                          {'EnvironmentFiles': '/private/SENSITIVE.conf (ignore_errors=yes)\nEnvironmentFiles=/etc/default/ssh (ignore_errors=yes)'},
                          {'ExecStart': '{ path=/private/SENSITIVE ; argv[]=/private/SENSITIVE ; }\nExecStart={ path=/usr/sbin/sshd ; argv[]=/usr/sbin/sshd -D $SSHD_OPTS ; }'},
                          {'Environment': 'SSHD_OPTS=-f/private/SENSITIVE.conf'},
                          {'PassEnvironment': 'SSHD_OPTS'}, {'RootDirectory': '/private/SENSITIVE'}):
            self.unit_overrides = overrides
            with self.subTest(overrides=overrides):
                with self.assertRaises((ValueError, RootActionError)) as raised:
                    self.manager.plan('ssh')
                self.assertNotIn('SENSITIVE', str(raised.exception))
        self.unit_overrides = {}
        self.manager_environment = 'SSHD_OPTS=-f/private/SENSITIVE.conf\n'
        with self.assertRaises((ValueError, RootActionError)) as raised:
            self.manager.plan('ssh')
        self.assertNotIn('SENSITIVE', str(raised.exception))

    def test_native_reload_must_check_syntax_then_signal_hup_without_restart(self):
        self.recommended()
        self.assertTrue(self.manager.plan('ssh'))
        for reload in ('{ path=/usr/bin/systemctl ; argv[]=/usr/bin/systemctl restart ssh.service ; }',
                       '{ path=/bin/kill ; argv[]=/bin/kill -HUP $MAINPID ; }',
                       '{ path=/bin/kill ; argv[]=/bin/kill -TERM $MAINPID ; }',
                       '{ path=/bin/kill ; argv[]=/bin/kill -HUP $MAINPID ; } ; { path=/usr/sbin/sshd ; argv[]=/usr/sbin/sshd -t ; }',
                       '{ path=/usr/sbin/sshd ; argv[]=/usr/sbin/sshd -t ; } ; { path=/bin/kill ; argv[]=/bin/kill -HUP $MAINPID ; } ; { path=/usr/bin/true ; argv[]=/usr/bin/true ; }'):
            self.unit_overrides = {'ExecReload': reload}
            with self.subTest(reload=reload), self.assertRaises((ValueError, RootActionError)):
                self.manager.plan('ssh')
        self.assertFalse(self.host.path(TARGET).exists())
        self.assertFalse(any(call[:2] == ['systemctl', 'reload'] for call in self.calls))

    def test_native_multiline_reload_array_preserves_both_commands_in_order(self):
        self.recommended()
        # Fresh Ubuntu 26.04 的 systemctl 將同一 a(sasbttttuii) property 印成兩行。
        syntax = '{ path=/usr/sbin/sshd ; argv[]=/usr/sbin/sshd -t ; ignore_errors=no ; start_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }'
        hup = '{ path=/bin/kill ; argv[]=/bin/kill -HUP $MAINPID ; ignore_errors=no ; start_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }'
        self.unit_overrides = {'ExecReload': syntax + '\nExecReload=' + hup}
        plan = self.manager.plan('ssh')
        self.assertEqual(plan['observations']['ssh_layout']['unit_layout']['exec_reload'],
                         [['/usr/sbin/sshd', '-t'], ['/bin/kill', '-HUP', '$MAINPID']])
        for value in (hup + '\nExecReload=' + syntax,
                      syntax + '\nExecReload=' + hup + '\nExecReload=' + hup):
            self.unit_overrides = {'ExecReload': value}
            with self.subTest(value=value), self.assertRaises((ValueError, RootActionError)):
                self.manager.plan('ssh')

    def test_external_auth_or_daemon_option_edits_block_plan_or_restore(self):
        self.recommended()
        self.manager.apply(self.manager.plan('ssh'))
        applied = self.host.path(TARGET).read_bytes()
        self.put('/etc/ssh/sshd_config.d/50-cloud-init.conf', 'PasswordAuthentication yes\n')
        with self.assertRaises(RootActionError):
            self.manager.restore_last('ssh')
        self.assertEqual(self.host.path(TARGET).read_bytes(), applied)
        self.put('/etc/default/ssh', 'SSHD_OPTS="-f /srv/custom-ssh.conf"\n')
        with self.assertRaises((ValueError, RootActionError)):
            self.manager.plan('ssh')

    def test_cli_accepts_ssh_plans_but_rejects_service_and_unconfirmed_mutations(self):
        self.recommended()
        from server_setting.__main__ import main
        def invoke(arguments):
            output = io.StringIO()
            with patch('server_setting.__main__.root_management_available', return_value=True), redirect_stdout(output):
                code = main(arguments, root_manager=self.manager)
            return code, json.loads(output.getvalue())
        code, result = invoke(['--root-plan', 'ssh'])
        self.assertEqual(code, 0)
        self.assertEqual(result['plan']['values'], FIELDS['ssh'])
        for arguments in (['--root-apply', 'ssh'], ['--root-plan', 'ssh', '--start'],
                          ['--root-apply', 'ssh', '--stop', '--confirm']):
            code, _ = invoke(arguments)
            self.assertNotEqual(code, 0)
        self.assertFalse(self.host.path(TARGET).exists())


if __name__ == '__main__':
    unittest.main()

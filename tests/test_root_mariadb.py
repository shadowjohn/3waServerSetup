"""Account SQL is an OS boundary; network file edits use real temporary files."""
import copy
from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from server_setting.root_actions import Host, RootActionError, RootManager
try:
    from server_setting import root_mariadb as security
except ImportError:
    security = None


def account(host, native='invalid', locked=False, socket=False, full=True):
    methods = "mysql_native_password USING '" + native + "'" + (' OR unix_socket' if socket else '')
    return {'priv': {'plugin': 'mysql_native_password', 'authentication_string': native,
                     'auth_or': [{}, {'plugin': 'unix_socket'}] if socket else [{}],
                     'account_locked': locked, 'access': 18446744073709551615 if full else 0},
            'create': f"CREATE USER `root`@`{host}` IDENTIFIED VIA {methods}" + (' ACCOUNT LOCK' if locked else ''),
            'grants': [f"GRANT {'ALL PRIVILEGES' if full else 'USAGE'} ON *.* TO `root`@`{host}`" + (' WITH GRANT OPTION' if full else '')]}


class MariaDbSecurityTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(security, 'MariaDB security transactions are not implemented')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'etc/mysql/mariadb.conf.d').mkdir(parents=True)
        (self.root / 'etc/os-release').write_text('ID=ubuntu\nVERSION_ID="26.04"\n')
        self.target = self.root / 'etc/mysql/mariadb.conf.d/99-3wa-server-access.cnf'
        self.target.write_bytes(b'# original network\r\n[mariadbd]\r\nbind-address=127.0.0.1\r\n')
        self.original = self.target.read_bytes()
        self.accounts = {'localhost': account('localhost', socket=True)}
        self.audit = False
        self.remote = False
        self.fail_network = False
        self.fail_password_probe = False
        self.events = []
        outer = self
        class FakeHost(Host):
            def run(self, argv, timeout=30):
                outer.events.append(('command', list(argv)))
                if argv[:2] == ['systemctl', 'restart']:
                    outer.remote = b'0.0.0.0' in outer.target.read_bytes()
                return subprocess.CompletedProcess(argv, 0, '', '')
            def mariadb_sql(self, sql, timeout=30):
                outer.events.append(('sql', sql))
                if 'INFORMATION_SCHEMA.PLUGINS' in sql:
                    return str(int(outer.audit)) + '\n'
                if 'FROM mysql.global_priv' in sql:
                    return ''.join(host + '\t' + json.dumps(value['priv'], separators=(',', ':')) + '\n' for host, value in sorted(outer.accounts.items()))
                if sql.startswith('SHOW CREATE USER') or sql.startswith('SHOW GRANTS'):
                    host = re.search(r"@['`]([^'`]+)['`]", sql)[1]
                    item = outer.accounts[host]
                    return (host + '\t' + item['create'] + '\n') if sql.startswith('SHOW CREATE') else '\n'.join(item['grants']) + '\n'
                for statement in sql.split(';'):
                    statement = statement.strip()
                    if not statement:
                        continue
                    match = re.search(r"@['`]([^'`]+)['`]", statement)
                    if match is None:
                        raise AssertionError('Unexpected SQL boundary: ' + statement.split()[0])
                    host = match[1]
                    if statement.startswith('DROP USER'):
                        outer.accounts.pop(host, None)
                        continue
                    if statement.startswith('CREATE USER'):
                        outer.accounts[host] = account(host, locked='ACCOUNT LOCK' in statement, full=False)
                    item = outer.accounts[host]
                    if statement.startswith(('CREATE USER', 'ALTER USER')):
                        native = re.search(r"USING '([^']*)'", statement)
                        if native:
                            item['priv']['authentication_string'] = native[1]
                            item['priv']['plugin'] = 'mysql_native_password'
                            item['priv']['auth_or'] = [{}, {'plugin': 'unix_socket'}] if 'unix_socket' in statement else [{}]
                        if 'ACCOUNT UNLOCK' in statement:
                            item['priv']['account_locked'] = False
                        elif 'ACCOUNT LOCK' in statement:
                            item['priv']['account_locked'] = True
                        if 'IDENTIFIED VIA' in statement:
                            item['create'] = statement.replace('ALTER USER', 'CREATE USER', 1)
                    elif statement.startswith('REVOKE ALL'):
                        item['grants'] = []
                    elif statement.startswith('GRANT '):
                        if 'ALL PRIVILEGES ON *.*' in statement:
                            item['priv']['access'] = 18446744073709551615
                        if statement not in item['grants']:
                            item['grants'].append(statement)
                    else:
                        raise AssertionError('Unexpected SQL boundary')
                return ''
            def mariadb_password_probe(self, password, port=3306, timeout=15):
                outer.events.append(('password_probe', None))
                if outer.fail_password_probe:
                    raise RootActionError('Password authentication failed.', 'MARIADB_PASSWORD_VERIFY_FAILED')
                return 'root@localhost\n'
        class Network:
            @staticmethod
            def network_status(host):
                return {'bind_address': '0.0.0.0' if outer.remote else '127.0.0.1',
                        'skip_networking': False, 'port': 3306, 'remote_ipv4': outer.remote,
                        'ipv4_listeners': [('0.0.0.0' if outer.remote else '127.0.0.1') + ':3306'],
                        'service_snapshot': {'unit': 'mariadb.service', 'active': True, 'enabled': True},
                        'health_scope': 'IPv4 daemon listener only'}
            @staticmethod
            def network_plan(host, enabled):
                content = '[mariadbd]\nbind-address=' + ('0.0.0.0' if enabled else '127.0.0.1') + '\n'
                before = outer.target.read_bytes()
                changed = before != content.encode()
                return {'schema': 1, 'enabled': enabled,
                        'files': [{'path': '/etc/mysql/mariadb.conf.d/99-3wa-server-access.cnf', 'before_sha256': hashlib.sha256(before).hexdigest(), 'content': content, 'mode': 0o644}],
                        'service_snapshot': {'unit': 'mariadb.service', 'active': True, 'enabled': True},
                        'validate': [['mariadbd', '--verbose', '--help']],
                        'apply_commands': [['systemctl', 'restart', 'mariadb.service']] if changed or enabled != outer.remote else [],
                        'observations': Network.network_status(host), 'health_scope': 'IPv4 daemon listener only'}
            @staticmethod
            def verify_network(host, enabled):
                outer.events.append(('verify_network', enabled))
                if outer.fail_network and enabled:
                    raise ValueError('fixture network check failed')
                if outer.remote != enabled:
                    raise ValueError('network does not match')
                return {'verified': True, **Network.network_status(host)}
        self.host = FakeHost(self.root)
        self.manager = RootManager(self.host)
        self.addCleanup(patch.stopall)
        patch.object(security, '_network', return_value=Network).start()
        patch('server_setting.root_actions.os.geteuid', return_value=0, create=True).start()
        @contextmanager
        def lock():
            yield
        patch.object(self.manager, '_lock', lock).start()

    def test_status_and_plan_never_expose_usable_authentication_material(self):
        native = '*' + 'A' * 40
        self.accounts['localhost'] = account('localhost', native, socket=True)
        status = self.manager.mariadb_security_status()
        plan = self.manager.mariadb_security_plan(True)
        self.assertTrue(status['local_socket_preserved'])
        self.assertTrue(status['local_password_set'])
        self.assertNotIn(native, json.dumps(status))
        self.assertNotIn(native, json.dumps(plan))
        self.assertEqual(plan['fingerprint'], self.manager.mariadb_security_plan(True)['fingerprint'])
        self.assertFalse((self.root / 'var').exists())

    def test_on_without_a_valid_native_password_requires_new_password(self):
        plan = self.manager.mariadb_security_plan(True)
        self.assertTrue(plan['password_required'])
        with self.assertRaises(RootActionError) as error:
            self.manager.mariadb_security_apply(plan)
        self.assertEqual(error.exception.code, 'MARIADB_PASSWORD_REQUIRED')
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_on_synchronizes_native_password_preserves_socket_and_unlocks_last(self):
        plan = self.manager.mariadb_security_plan(True, True)
        result = self.manager.mariadb_security_apply(plan, password='fixture password')
        self.assertEqual(result['status'], 'MARIADB_SECURITY_APPLIED')
        native = self.accounts['localhost']['priv']['authentication_string']
        self.assertEqual(native, self.accounts['%']['priv']['authentication_string'])
        self.assertNotEqual(native, 'invalid')
        self.assertTrue(any(item.get('plugin') == 'unix_socket' for item in self.accounts['localhost']['priv']['auth_or']))
        self.assertFalse(self.accounts['%']['priv']['account_locked'])
        verify = self.events.index(('verify_network', True))
        unlock = next(index for index, item in enumerate(self.events) if item[0] == 'sql' and "'root'@'%' ACCOUNT UNLOCK" in item[1])
        self.assertLess(verify, unlock)
        for path in (self.root / 'var/lib/3wa-server-setup/transactions').rglob('*.json'):
            text = path.read_text()
            self.assertNotIn('fixture password', text)
            self.assertNotIn(native, text)

    def test_off_locks_before_network_restart(self):
        self.accounts['localhost'] = account('localhost', '*' + 'B' * 40, socket=True)
        self.accounts['%'] = account('%', '*' + 'B' * 40)
        self.remote = True
        result = self.manager.mariadb_security_apply(self.manager.mariadb_security_plan(False))
        self.assertEqual(result['status'], 'MARIADB_SECURITY_APPLIED')
        self.assertTrue(self.accounts['%']['priv']['account_locked'])
        lock = next(index for index, item in enumerate(self.events) if item[0] == 'sql' and "'root'@'%' ACCOUNT LOCK" in item[1])
        restart = next(index for index, item in enumerate(self.events) if item[0] == 'command' and item[1][:2] == ['systemctl', 'restart'])
        self.assertLess(lock, restart)

    def test_password_change_off_also_synchronizes_a_locked_remote_account(self):
        result = self.manager.mariadb_security_apply(self.manager.mariadb_security_plan(False, True), password='new fixture')
        self.assertEqual(result['status'], 'MARIADB_SECURITY_APPLIED')
        self.assertEqual(self.accounts['localhost']['priv']['authentication_string'], self.accounts['%']['priv']['authentication_string'])
        self.assertTrue(self.accounts['%']['priv']['account_locked'])
        self.assertFalse(self.remote)

    def test_password_only_keeps_wide_listener_and_does_not_create_remote_account(self):
        self.remote = True
        before = self.target.read_bytes()
        result = self.manager.mariadb_security_apply(self.manager.mariadb_security_plan(None, True), password='keep listener')
        self.assertEqual(result['status'], 'MARIADB_SECURITY_APPLIED')
        self.assertTrue(self.remote)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertNotIn('%', self.accounts)
        self.assertFalse(any(item[0] == 'command' and item[1][:2] == ['systemctl', 'restart'] for item in self.events))

    def test_password_only_preserves_existing_remote_lock_and_grants(self):
        self.remote = True
        self.accounts['%'] = account('%', '*' + 'D' * 40, locked=True, full=False)
        grants = copy.deepcopy(self.accounts['%']['grants'])
        self.manager.mariadb_security_apply(self.manager.mariadb_security_plan(None, True), password='keep remote policy')
        self.assertTrue(self.accounts['%']['priv']['account_locked'])
        self.assertEqual(self.accounts['%']['grants'], grants)
        self.assertEqual(self.accounts['%']['priv']['authentication_string'], self.accounts['localhost']['priv']['authentication_string'])
        mutations = [item[1] for item in self.events if item[0] == 'sql' and not item[1].startswith(('SHOW', 'SELECT'))]
        self.assertFalse(any('ACCOUNT LOCK' in sql or 'ACCOUNT UNLOCK' in sql or sql.startswith(('GRANT', 'REVOKE', 'CREATE')) for sql in mutations))

    def test_unknown_remote_auth_rejects_password_rotation_but_allows_explicit_off_lock(self):
        self.remote = True
        self.accounts['%'] = account('%')
        self.accounts['%']['priv']['plugin'] = 'ed25519'
        self.accounts['%']['create'] = "CREATE USER `root`@`%` IDENTIFIED VIA ed25519 USING 'fixture-nonnative'"
        with self.assertRaises(RootActionError):
            self.manager.mariadb_security_plan(None, True)
        result = self.manager.mariadb_security_apply(self.manager.mariadb_security_plan(False))
        self.assertEqual(result['status'], 'MARIADB_SECURITY_APPLIED')
        self.assertEqual(self.accounts['%']['priv']['plugin'], 'ed25519')
        self.assertTrue(self.accounts['%']['priv']['account_locked'])

    def test_on_reusing_native_hash_does_not_claim_password_login_verification(self):
        self.accounts['localhost'] = account('localhost', '*' + 'E' * 40, socket=True)
        result = self.manager.mariadb_security_apply(self.manager.mariadb_security_plan(True))
        self.assertFalse(result['verification']['password_login_verified'])
        self.assertFalse(any(item[0] == 'password_probe' for item in self.events))

    def test_unknown_listener_baseline_is_rejected_before_any_password_change(self):
        self.remote = None
        self.accounts['%'] = account('%', native='*' + 'A' * 40)
        self.assertIsNone(self.manager.mariadb_security_status()['remote_enabled'])
        with self.assertRaises(RootActionError) as error:
            self.manager.mariadb_security_plan(None, True)
        self.assertEqual(error.exception.code, 'MARIADB_UNSUPPORTED')
        self.assertFalse(any(item[0] == 'sql' and item[1].startswith('ALTER') for item in self.events))

    def test_unknown_active_auth_is_reported_unblocked_instead_of_external_off(self):
        self.remote = True
        self.accounts['%'] = account('%')
        self.accounts['%']['priv']['plugin'] = 'ed25519'
        status = self.manager.mariadb_security_status()
        self.assertTrue(status['remote_enabled'])
        self.assertIsNone(status['root_remote_password_set'])

    def test_empty_native_password_and_limited_privileges_are_not_external_off(self):
        self.remote = True
        self.accounts['%'] = account('%', native='', full=False)
        status = self.manager.mariadb_security_status()
        self.assertTrue(status['remote_enabled'])
        self.assertFalse(status['root_remote_password_set'])
        self.assertFalse(status['root_remote_full_privileges'])
        self.accounts['%']['priv']['account_locked'] = True
        self.assertFalse(self.manager.mariadb_security_status()['remote_enabled'])

    def test_socket_only_and_custom_listener_layouts_are_rejected_before_ddl(self):
        network = security._network()
        observed = network.network_status(self.host)
        for patch_values in ({'skip_networking': True, 'ipv4_listeners': [], 'remote_ipv4': False},
                             {'bind_address': '192.0.2.10', 'ipv4_listeners': ['192.0.2.10:3306'], 'remote_ipv4': True}):
            with self.subTest(layout=patch_values), patch.object(network, 'network_status', return_value={**observed, **patch_values}):
                self.assertFalse(self.manager.mariadb_security_status()['supported'])
                with self.assertRaises(RootActionError) as error:
                    self.manager.mariadb_security_plan(None, True)
                self.assertEqual(error.exception.code, 'MARIADB_UNSUPPORTED')
                self.assertFalse(any(item[0] == 'sql' and item[1].startswith(('ALTER', 'CREATE', 'GRANT')) for item in self.events))

    def test_stale_account_and_tampered_plan_are_rejected_before_writes(self):
        plan = self.manager.mariadb_security_plan(True, True)
        modified = copy.deepcopy(plan)
        modified['remote_enabled'] = False
        with self.assertRaises(RootActionError):
            self.manager.mariadb_security_apply(modified, password='test')
        self.accounts['localhost']['priv']['authentication_string'] = '*' + 'C' * 40
        with self.assertRaises(RootActionError):
            self.manager.mariadb_security_apply(plan, password='test')
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_network_failure_restores_original_auth_and_file_bytes(self):
        original_auth = copy.deepcopy(self.accounts)
        self.fail_network = True
        with self.assertRaises(RootActionError) as error:
            self.manager.mariadb_security_apply(self.manager.mariadb_security_plan(True, True), password='fixture rollback')
        self.assertEqual(error.exception.code, 'MARIADB_ROLLED_BACK')
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual(set(self.accounts), {'localhost'})
        self.assertEqual(self.accounts['localhost']['priv']['authentication_string'], original_auth['localhost']['priv']['authentication_string'])
        self.assertNotIn('fixture rollback', str(error.exception))

    def test_failed_strict_password_probe_rolls_back(self):
        self.fail_password_probe = True
        with self.assertRaises(RootActionError) as error:
            self.manager.mariadb_security_apply(self.manager.mariadb_security_plan(True, True), password='fixture verify')
        self.assertEqual(error.exception.code, 'MARIADB_ROLLED_BACK')
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_active_audit_plugin_blocks_credential_reads_and_ddl(self):
        self.audit = True
        with self.assertRaises(RootActionError) as error:
            self.manager.mariadb_security_plan(True, True)
        self.assertEqual(error.exception.code, 'MARIADB_AUDIT_ACTIVE')
        self.assertFalse(any(item[0] == 'sql' and ('global_priv' in item[1] or 'ALTER USER' in item[1]) for item in self.events))

    def test_password_is_bound_to_review_and_validated_without_trimming(self):
        for password in ('', 'bad\nvalue', 'bad\x00value', 'x' * 4097):
            with self.subTest(password_length=len(password)), self.assertRaises(RootActionError):
                self.manager.mariadb_security_apply(self.manager.mariadb_security_plan(False, True), password=password)
        with self.assertRaises(RootActionError):
            self.manager.mariadb_security_apply(self.manager.mariadb_security_plan(False), password='unreviewed')

    def test_credentials_are_excluded_from_generic_restore(self):
        self.manager.mariadb_security_apply(self.manager.mariadb_security_plan(True, True), password='fixture restore boundary')
        with self.assertRaises(RootActionError) as error:
            self.manager.restore_last('mariadb')
        self.assertEqual(error.exception.code, 'NO_BACKUP')


class SecretRunnerTests(unittest.TestCase):
    def test_sensitive_sql_is_sent_only_after_session_guards_acknowledge(self):
        host = Host()
        self.assertTrue(hasattr(host, 'mariadb_sql'), 'Secret stdin runner is not implemented')
        class Child:
            def __init__(self):
                self.stdin = io.StringIO()
                self.stdout = io.StringIO('__3WA_SQL_READY__\n')
                self.returncode = 0
                self.sent = []
            def communicate(self, input=None, timeout=None):
                self.sent.append(input)
                if input is not None:
                    guards = self.stdin.getvalue()
                    for expected in ('sql_log_off=1', 'sql_log_bin=0', 'log_slow_query=0'):
                        if expected not in guards:
                            raise AssertionError('DDL arrived before session guards')
                return 'safe-result\n', ''
            def kill(self):
                self.returncode = -9
        child = Child()
        ddl = "ALTER USER 'root'@'%' IDENTIFIED VIA mysql_native_password USING '*" + 'F' * 40 + "';"
        with patch.object(host, '_mariadb_client', return_value='/usr/bin/mariadb'), \
                patch('server_setting.root_actions.subprocess.Popen', return_value=child) as launch:
            self.assertEqual(host.mariadb_sql(ddl), 'safe-result\n')
        argv = launch.call_args.args[0]
        self.assertNotIn(ddl, repr(argv))
        self.assertIn('--no-defaults', argv)
        self.assertIn('--protocol=socket', argv)
        self.assertIn('--skip-reconnect', argv)
        self.assertEqual(child.sent, [ddl + '\n'])

    def test_failed_session_guard_never_receives_sensitive_sql(self):
        host = Host()
        self.assertTrue(hasattr(host, 'mariadb_sql'), 'Secret stdin runner is not implemented')
        class Child:
            stdin = io.StringIO()
            stdout = io.StringIO('__3WA_SQL_AUDIT__\n')
            returncode = 0
            sent = []
            def communicate(self, input=None, timeout=None):
                self.sent.append(input)
                return '', 'fixture native secret diagnostic'
            def kill(self):
                self.returncode = -9
        child = Child()
        with patch.object(host, '_mariadb_client', return_value='/usr/bin/mariadb'), \
                patch('server_setting.root_actions.subprocess.Popen', return_value=child):
            with self.assertRaises(RootActionError) as error:
                host.mariadb_sql('SECRET DDL')
        self.assertEqual(error.exception.code, 'MARIADB_AUDIT_ACTIVE')
        self.assertFalse(any(item is not None for item in child.sent))
        self.assertNotIn('fixture native secret diagnostic', str(error.exception))

    def test_password_probe_uses_only_private_memfd_and_tcp(self):
        host = Host()
        self.assertTrue(hasattr(host, 'mariadb_password_probe'), 'Memory-only password probe is not implemented')
        writes = []
        with patch.object(host, '_mariadb_client', return_value='/usr/bin/mariadb'), \
                patch('server_setting.root_actions.os.memfd_create', return_value=99, create=True), \
                patch('server_setting.root_actions.os.fchmod', create=True) as chmod, \
                patch('server_setting.root_actions.os.write', side_effect=lambda descriptor, data: writes.append(data) or len(data)), \
                patch('server_setting.root_actions.os.lseek'), patch('server_setting.root_actions.os.close'), \
                patch('server_setting.root_actions.subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'root@localhost\n', '')) as run:
            self.assertEqual(host.mariadb_password_probe('fixture " secret\\value'), 'root@localhost\n')
        chmod.assert_called_once_with(99, 0o600)
        argv = run.call_args.args[0]
        self.assertIn('--protocol=tcp', argv)
        self.assertIn('--host=127.0.0.1', argv)
        self.assertIn('--defaults-file=/proc/self/fd/99', argv)
        self.assertNotIn('fixture', repr(argv))
        self.assertNotIn('fixture', repr(run.call_args.kwargs['env']))
        self.assertEqual(run.call_args.kwargs['pass_fds'], (99,))
        self.assertIn(b'fixture', b''.join(writes))


if __name__ == '__main__':
    unittest.main()

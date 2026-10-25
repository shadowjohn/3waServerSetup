"""Real configuration files with only native command/proc-link boundaries replaced."""
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from server_setting.root_actions import Host, RootActionError

if importlib.util.find_spec('server_setting.mariadb_network'):
    from server_setting import mariadb_network as network
else:
    network = None


TCP_HEADER = '  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n'
TCP_LOOPBACK = '  0: 0100007F:0CEA 00000000:0000 0A 00000000:00000000 00:00000000 00000000 113 0 555 1\n'
TCP_ALL = '  0: 00000000:0CEA 00000000:0000 0A 00000000:00000000 00:00000000 00000000 113 0 555 1\n'
ACCESS = '/etc/mysql/mariadb.conf.d/99-3wa-server-access.cnf'
MARKER = '# Managed by 3waServerSetup; MariaDB network access only.'


class NetworkHost(Host):
    def __init__(self, root):
        super().__init__(root)
        self.commands = []
        self.runtime = {'bind_address': '127.0.0.1', 'skip_networking': 'OFF', 'port': '3306', 'wsrep_on': 'OFF'}
        self.properties = {'LoadState': 'loaded', 'ActiveState': 'active', 'UnitFileState': 'enabled',
            'MainPID': '123', 'User': 'mysql', 'FragmentPath': '/usr/lib/systemd/system/mariadb.service',
            'DropInPaths': '', 'Environment': '', 'EnvironmentFiles': '/run/mysqld/wsrep-start-position (ignore_errors=yes)',
            'PassEnvironment': '', 'RootDirectory': '', 'RootImage': '', 'TriggeredBy': '',
            'ExecStart': '{ path=/usr/sbin/mariadbd ; argv[]=/usr/sbin/mariadbd $MYSQLD_OPTS $_WSREP_NEW_CLUSTER $_WSREP_START_POSITION ; ignore_errors=no ; pid=123 ; }'}
        self.manager_environment = ''
        self.units = 'mariadb.service loaded active running MariaDB database server\n'
        self.fd_links = {}
        self.fail_command = None

    def put(self, path, text):
        target = self.path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8', newline='')

    def run(self, argv, timeout=30):
        self.commands.append(list(argv))
        if argv[0] == self.fail_command:
            return subprocess.CompletedProcess(argv, 1, 'SECRET_NATIVE_OUTPUT', 'SECRET_NATIVE_ERROR')
        if argv[:2] == ['systemctl', 'show']:
            output = ''.join(key + '=' + value + '\n' for key, value in self.properties.items())
        elif argv[:2] == ['systemctl', 'show-environment']:
            output = self.manager_environment
        elif argv[:2] == ['systemctl', 'list-units']:
            output = self.units
        elif argv[0] == 'mariadb':
            output = ''.join(key + '\t' + value + '\n' for key, value in self.runtime.items())
        else:
            output = ''
        return subprocess.CompletedProcess(argv, 0, output, '')


class MariaDBNetworkTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(network, 'MariaDB network plan API is not implemented')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.host = NetworkHost(self.tmp.name)
        self.host.put('/etc/mysql/my.cnf', '[client-server]\n!includedir /etc/mysql/mariadb.conf.d/\n')
        self.host.put('/etc/mysql/mariadb.conf.d/50-server.cnf', '[mariadbd]\nbind-address=127.0.0.1\n')
        self.host.put('/etc/mysql/mariadb.conf.d/99-3wa-server-setup.cnf', '# old tool slow-log settings\n[mariadbd]\nslow_query_log=1\n')
        self.host.put('/usr/lib/systemd/system/mariadb.service', '[Service]\nUser=mysql\n')
        self.host.put('/proc/123/comm', 'mariadbd\n')
        self.host.put('/proc/123/status', 'Name:\tmariadbd\nUid:\t113\t113\t113\t113\n')
        self.host.put('/proc/123/fd/9', '')
        self.host.fd_links[self.host.path('/proc/123/fd/9')] = 'socket:[555]'
        self.tcp_path = f'/proc/{os.getpid()}/net/tcp'
        self.host.put(self.tcp_path, TCP_HEADER + TCP_LOOPBACK)
        path_type = type(self.host.root)
        original_link, original_readlink = path_type.is_symlink, path_type.readlink
        self.addCleanup(patch.stopall)
        patch.object(path_type, 'is_symlink', lambda path: path in self.host.fd_links or original_link(path)).start()
        patch.object(path_type, 'readlink', lambda path: Path(self.host.fd_links[path]) if path in self.host.fd_links else original_readlink(path)).start()

    def test_on_off_plans_only_manage_the_new_access_dropin_and_do_not_write(self):
        slow = self.host.path('/etc/mysql/mariadb.conf.d/99-3wa-server-setup.cnf').read_bytes()
        for enabled, bind in ((True, '0.0.0.0'), (False, '127.0.0.1')):
            with self.subTest(enabled=enabled):
                plan = network.network_plan(self.host, enabled)
                self.assertEqual(len(plan['files']), 1)
                change = plan['files'][0]
                self.assertEqual(change['path'], ACCESS)
                self.assertIsNone(change['before_sha256'])
                self.assertEqual(change['content'], MARKER + '\n[mariadbd]\nbind-address = ' + bind + '\nskip-networking = 0\n')
                self.assertEqual(plan['validate'], [['mariadbd', '--defaults-file=/etc/mysql/my.cnf', '--verbose', '--help']])
                self.assertEqual(plan['apply_commands'], [['systemctl', 'restart', 'mariadb.service']])
                self.assertTrue(plan['service_snapshot']['active'])
        self.assertFalse(self.host.path(ACCESS).exists())
        self.assertEqual(self.host.path('/etc/mysql/mariadb.conf.d/99-3wa-server-setup.cnf').read_bytes(), slow)
        self.assertFalse(any(command[0] in {'mariadbd', 'apt-get'} or command[:2] == ['systemctl', 'restart'] for command in self.host.commands))

    def test_existing_tool_file_hash_and_noop_do_not_restart(self):
        content = MARKER + '\n[mariadbd]\nbind-address = 127.0.0.1\nskip-networking = 0\n'
        self.host.put(ACCESS, content)
        plan = network.network_plan(self.host, False)
        self.assertEqual(plan['files'][0]['before_sha256'], hashlib.sha256(content.encode()).hexdigest())
        self.assertEqual(plan['apply_commands'], [])
        self.host.runtime['bind_address'] = '0.0.0.0'
        self.host.put(self.tcp_path, TCP_HEADER + TCP_ALL)
        self.assertEqual(network.network_plan(self.host, False)['apply_commands'], [['systemctl', 'restart', 'mariadb.service']])

    def test_unknown_access_file_is_not_overwritten(self):
        content = '# administrator custom networking\n[mariadbd]\nbind-address=10.1.1.1\n'
        self.host.put(ACCESS, content)
        with self.assertRaisesRegex(ValueError, 'ownership|unknown|managed'):
            network.network_plan(self.host, True)
        self.assertEqual(self.host.path(ACCESS).read_text(), content)

    def test_native_environment_file_accepts_only_safe_recovery_position(self):
        self.host.put('/run/mysqld/wsrep-start-position', "_WSREP_START_POSITION='--wsrep_start_position=00000000-0000-0000-0000-000000000000:-1'\n")
        self.assertEqual(network.network_plan(self.host, False)['enabled'], False)
        self.host.put('/run/mysqld/wsrep-start-position', 'MYSQLD_OPTS=--bind-address=0.0.0.0\n')
        with self.assertRaisesRegex(ValueError, 'environment|recovery'):
            network.network_plan(self.host, False)

    def test_custom_unit_environment_config_and_instances_are_rejected(self):
        cases = [('DropInPaths', '/etc/systemd/system/mariadb.service.d/custom.conf'),
                 ('EnvironmentFiles', '/etc/custom/mysql-secret-env (ignore_errors=yes)'),
                 ('Environment', 'MYSQLD_OPTS=SECRET_OPTIONS'),
                 ('ExecStart', '{ path=/opt/custom/mariadbd ; argv[]=/opt/custom/mariadbd ; }'),
                 ('FragmentPath', '/etc/systemd/system/mariadb.service'),
                 ('RootDirectory', '/srv/chroot'), ('TriggeredBy', 'mariadb.socket'),
                 ('MainPID', '0'), ('ActiveState', 'inactive')]
        original = dict(self.host.properties)
        for key, value in cases:
            with self.subTest(key=key):
                self.host.properties = {**original, key: value}
                with self.assertRaises(ValueError) as failure:
                    network.network_plan(self.host, True)
                self.assertNotIn('SECRET_OPTIONS', str(failure.exception))
        self.host.properties = original
        self.host.units += 'mariadb@other.service loaded inactive dead Other database\n'
        with self.assertRaisesRegex(ValueError, 'instance|unit'):
            network.network_plan(self.host, True)
        self.host.units = 'mariadb.service loaded active running MariaDB\n'
        self.host.put('/proc/456/comm', 'mysqld\n')
        with self.assertRaisesRegex(ValueError, 'instance|process'):
            network.network_plan(self.host, True)

    def test_safe_runtime_observations_and_true_ipv4_listener_verification(self):
        off = network.verify_network(self.host, False)
        self.assertTrue(off['verified'])
        self.assertEqual(off['bind_address'], '127.0.0.1')
        self.assertIs(off['skip_networking'], False)
        self.assertEqual(off['port'], 3306)
        self.assertEqual(off['ipv4_listeners'], ['127.0.0.1:3306'])
        self.host.runtime['bind_address'] = '0.0.0.0'
        self.host.put(self.tcp_path, TCP_HEADER + TCP_ALL)
        on = network.verify_network(self.host, True)
        self.assertTrue(on['verified'])
        self.assertEqual(on['ipv4_listeners'], ['0.0.0.0:3306'])
        self.assertNotIn('SECRET', str(network.network_status(self.host)))
        query = next(command for command in self.host.commands if command[0] == 'mariadb')
        self.assertEqual(query[1:3], ['--no-defaults', '--protocol=socket'])
        self.assertNotIn('global_priv', ' '.join(query))
        self.assertIn('SHOW GLOBAL VARIABLES', ' '.join(query))

    def test_runtime_mismatch_missing_or_other_process_listener_is_never_verified(self):
        self.host.runtime['skip_networking'] = 'ON'
        with self.assertRaisesRegex(ValueError, 'skip|network'):
            network.verify_network(self.host, False)
        self.host.runtime['skip_networking'] = 'OFF'
        self.host.put(self.tcp_path, TCP_HEADER)
        with self.assertRaisesRegex(ValueError, 'listen|LISTEN'):
            network.verify_network(self.host, False)
        self.host.put(self.tcp_path, TCP_HEADER + TCP_LOOPBACK.replace(' 555 ', ' 999 '))
        with self.assertRaisesRegex(ValueError, 'listen|socket|process'):
            network.verify_network(self.host, False)

    def test_empty_or_malformed_proc_evidence_is_rejected_as_unverifiable(self):
        for text in ('', TCP_HEADER + 'malformed row\n'):
            with self.subTest(text=text):
                self.host.put(self.tcp_path, text)
                with self.assertRaisesRegex(ValueError, 'evidence|Malformed|table|LISTEN'):
                    network.verify_network(self.host, False)

    def test_custom_defaults_include_and_manager_options_are_rejected_before_file_changes(self):
        self.host.put('/etc/mysql/my.cnf', '[mariadbd]\n!include /opt/custom/mysql.cnf\n')
        with self.assertRaisesRegex(ValueError, 'include|layout'):
            network.network_plan(self.host, True)
        self.host.put('/etc/mysql/my.cnf', '[client-server]\n!includedir /etc/mysql/mariadb.conf.d/\n')
        self.host.manager_environment = 'PATH=/usr/bin\nMYSQLD_OPTS=--defaults-file=/secret/SECRET_CONFIG\n'
        with self.assertRaisesRegex(ValueError, 'environment') as failure:
            network.network_plan(self.host, True)
        self.assertNotIn('SECRET_CONFIG', str(failure.exception))
        self.assertFalse(self.host.path(ACCESS).exists())

    def test_nonstandard_port_cluster_or_boolean_inputs_are_rejected(self):
        for value in ('yes', 1, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                network.network_plan(self.host, value)
        self.host.runtime['port'] = '3307'
        with self.assertRaisesRegex(ValueError, '3306|port'):
            network.network_plan(self.host, True)
        self.host.runtime['port'] = '3306'
        self.host.runtime['wsrep_on'] = 'ON'
        with self.assertRaisesRegex(ValueError, 'cluster|Galera|wsrep'):
            network.network_plan(self.host, True)

    def test_native_failures_do_not_disclose_raw_diagnostics(self):
        self.host.fail_command = 'mariadb'
        with self.assertRaises(ValueError) as failure:
            network.network_plan(self.host, True)
        self.assertNotIn('SECRET_NATIVE', str(failure.exception))


if __name__ == '__main__':
    unittest.main()

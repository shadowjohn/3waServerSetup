import importlib
import os
import stat
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from server_setting.model import FIELDS


class RecipeHost:
    """Only commands are substituted; configuration reads and edits use disk."""
    def __init__(self, root):
        self.root = Path(root)
        self.commands = []
        self.active = True
        self.enabled = True
        self.installed = True
        self.timezone = 'Etc/UTC'
        self.effective = {}
        self.reads = []
        self.fpm_unit_overrides = {}
        self.manager_environment = ''
        self.apache_include_output = ('Included configuration files:\n'
            '  (*) /etc/apache2/apache2.conf\n'
            '    (222) /etc/apache2/conf-enabled/zz-3wa-server-setup.conf\n'
            '      (2) /etc/apache2/conf-available/zz-3wa-server-setup.conf\n')

    def path(self, path):
        if not path.startswith('/') or '..' in Path(path).parts:
            raise ValueError('unsafe path')
        result = self.root / path.lstrip('/')
        for part in (result, *result.parents):
            if part == self.root.parent:
                break
            if part.is_symlink():
                raise ValueError('symlink')
        return result

    def read(self, path):
        self.reads.append(path)
        result = self.path(path)
        return result.read_text(encoding='utf-8') if result.is_file() else None

    def put(self, path, text):
        result = self.path(path)
        result.parent.mkdir(parents=True, exist_ok=True)
        result.write_text(text, encoding='utf-8', newline='')

    def run(self, argv, timeout=30):
        self.commands.append(list(argv))
        code, out = 0, ''
        if argv[0] == 'dpkg-query':
            code, out = (0, 'install ok installed\n') if self.installed else (1, '')
        elif argv[:2] == ['systemctl', 'show'] and '--property=ExecStart' in argv:
            properties = {'DropInPaths': '', 'EnvironmentFiles': '', 'Environment': '',
                          'PassEnvironment': '', 'RootDirectory': '', 'RootImage': '',
                          'ExecStart': '{ path=/usr/sbin/php-fpm8.5 ; argv[]=/usr/sbin/php-fpm8.5 --nodaemonize --fpm-config /etc/php/8.5/fpm/php-fpm.conf ; ignore_errors=no ; start_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }'}
            properties.update(self.fpm_unit_overrides)
            # Native systemctl prints no row for an empty a(sb) array even
            # with --all; an empty Environment as array needs --all.
            if not properties['EnvironmentFiles']:
                del properties['EnvironmentFiles']
            if '--all' not in argv and not properties['Environment']:
                del properties['Environment']
            out = ''.join(key + '=' + value + '\n' for key, value in properties.items())
        elif argv[:2] == ['systemctl', 'show-environment']:
            out = self.manager_environment
        elif argv[:2] == ['systemctl', 'show']:
            out = ('LoadState=loaded\nActiveState=' + ('active' if self.active else 'inactive')
                   + '\nUnitFileState=' + ('enabled' if self.enabled else 'disabled') + '\n')
        elif argv[:2] == ['timedatectl', 'show']:
            out = 'Timezone=' + self.timezone + '\nNTPSynchronized=no\n'
        elif argv[:2] == ['timedatectl', 'list-timezones']:
            out = 'Asia/Taipei\nEtc/UTC\nUTC\n'
        elif argv[0] == 'mariadb':
            out = ''.join(key + '\t' + value + '\n' for key, value in self.effective.items())
        elif argv == ['apache2ctl', '-t', '-D', 'DUMP_INCLUDES']:
            out = self.apache_include_output
        elif argv[0] == 'testparm' and any(arg.startswith('--parameter-name=') for arg in argv):
            key = next(arg.split('=', 1)[1] for arg in argv if arg.startswith('--parameter-name='))
            out = self.effective.get(key, '') + '\n'
        return subprocess.CompletedProcess(argv, code, out, '')


class RecipesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.recipes = importlib.import_module('server_setting.root_recipes')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.host = RecipeHost(self.tmp.name)
        self.host.put('/etc/apache2/apache2.conf', 'ServerRoot /etc/apache2\nIncludeOptional conf-enabled/*.conf\n')
        self.host.put('/etc/vsftpd.conf', '# untouched comment\nlisten=NO\nlisten_ipv6=YES\nwrite_enable=NO\nxferlog_enable=YES\n')
        self.host.put('/etc/samba/smb.conf', '[global]\nworkgroup = CUSTOM\nsecurity = user\n[photos]\npath = /srv/photos\nread only = yes\n')
        self.host.put('/etc/mysql/my.cnf', '[client-server]\n!includedir /etc/mysql/mariadb.conf.d/\n')
        self.seed_fpm()

    def seed_fpm(self, version='8.5'):
        base = '/etc/php/' + version + '/fpm/'
        self.host.put(base + 'php.ini', '[PHP]\nauto_prepend_file =\nmemory_limit=32M\n')
        self.host.put(base + 'php-fpm.conf', '[global]\ninclude=/etc/php/' + version + '/fpm/pool.d/*.conf\n')
        self.host.put(base + 'pool.d/www.conf', '[www]\nuser = www-data\ngroup = www-data\nlisten = /run/php/php' + version + '-fpm.sock\n')
        self.host.path(base + 'conf.d').mkdir(parents=True, exist_ok=True)

    def write_plan(self, plan):
        for change in plan['files']:
            self.host.put(change['path'], change['content'])

    def test_six_modules_have_concrete_plans_and_no_preview_writes(self):
        before = {str(p.relative_to(self.host.root)): p.read_bytes() for p in self.host.root.rglob('*') if p.is_file()}
        for module in ('apache', 'php', 'vsftpd', 'samba', 'mariadb', 'time'):
            with self.subTest(module=module):
                plan = self.recipes.build_plan(self.host, module, dict(FIELDS[module]))
                self.assertEqual(plan['module'], module)
                self.assertEqual(plan['values'], FIELDS[module])
                self.assertTrue(plan['files'] or plan['apply_commands'])
                self.assertTrue(plan['health_scope'])
        after = {str(p.relative_to(self.host.root)): p.read_bytes() for p in self.host.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_ftp_preserves_unmanaged_lines_and_replaces_only_selected_fields(self):
        plan = self.recipes.build_plan(self.host, 'vsftpd', dict(FIELDS['vsftpd']))
        self.write_plan(plan)
        content = self.host.read('/etc/vsftpd.conf')
        self.assertIn('# untouched comment\nlisten=NO\nlisten_ipv6=YES\n', content)
        self.assertIn('xferlog_enable=YES\n', content)
        self.assertNotIn('write_enable=NO', content)
        self.assertIn('write_enable=YES', content)
        self.assertEqual(plan['validate'], [])
        verified = self.recipes.verify_plan(self.host, plan)
        self.assertTrue(verified['verified'])
        self.assertIn('login', verified['detail'].lower())

    def test_unmanaged_native_config_lines_keep_original_line_endings(self):
        cases = [('vsftpd', '/etc/vsftpd.conf', '# custom\r\nlisten=YES\r\nwrite_enable=NO\r\n', b'# custom\r\nlisten=YES\r\n'),
                 ('samba', '/etc/samba/smb.conf', '[global]\r\nworkgroup = CUSTOM\r\n[photos]\r\npath = /srv/photos\r\n', b'[photos]\r\npath = /srv/photos\r\n')]
        for module, path, content, preserved in cases:
            with self.subTest(module=module):
                self.host.put(path, content)
                plan = self.recipes.build_plan(self.host, module, dict(FIELDS[module]))
                self.write_plan(plan)
                self.assertIn(preserved, self.host.path(path).read_bytes())

    def test_samba_preserves_shares_and_checks_effective_globals(self):
        self.host.effective = {'security': 'USER', 'map to guest': 'Never'}
        plan = self.recipes.build_plan(self.host, 'samba', dict(FIELDS['samba']))
        self.write_plan(plan)
        content = self.host.read('/etc/samba/smb.conf')
        self.assertIn('workgroup = CUSTOM\n', content)
        self.assertIn('[photos]\npath = /srv/photos\nread only = yes\n', content)
        self.assertTrue(self.recipes.verify_plan(self.host, plan)['verified'])
        self.host.effective['map to guest'] = 'Bad User'
        with self.assertRaisesRegex(ValueError, 'map to guest'):
            self.recipes.verify_plan(self.host, plan)

    def test_apache_tool_config_is_included_and_unrelated_files_remain(self):
        self.host.put('/etc/apache2/conf-available/security.conf', '# custom\nServerTokens Full\n')
        plan = self.recipes.build_plan(self.host, 'apache', dict(FIELDS['apache']))
        self.write_plan(plan)
        available = next(f for f in plan['files'] if '/conf-available/' in f['path'])
        enabled = next(f for f in plan['files'] if '/conf-enabled/' in f['path'])
        self.assertIn(available['path'], enabled['content'])
        self.assertEqual(self.host.read('/etc/apache2/conf-available/security.conf'), '# custom\nServerTokens Full\n')
        self.assertIn(['apache2ctl', 'configtest'], plan['validate'])
        self.host.put('/etc/apache2/apache2.conf', '# no native include\n')
        with self.assertRaisesRegex(ValueError, 'include|Include'):
            self.recipes.build_plan(self.host, 'apache', dict(FIELDS['apache']))

    def test_apache_checks_loaded_include_evidence_instead_of_source_text_only(self):
        plan = self.recipes.build_plan(self.host, 'apache', dict(FIELDS['apache']))
        self.write_plan(plan)
        self.assertTrue(self.recipes.verify_plan(self.host, plan)['verified'])
        self.host.apache_include_output = 'Included configuration files:\n  (*) /etc/apache2/apache2.conf\n'
        with self.assertRaisesRegex(ValueError, 'include|loaded|parsed'):
            self.recipes.verify_plan(self.host, plan)

    def test_php_detects_version_unit_socket_and_uses_fpm_evidence(self):
        plan = self.recipes.build_plan(self.host, 'php', dict(FIELDS['php']))
        self.assertEqual(plan['service_snapshot']['unit'], 'php8.5-fpm.service')
        self.assertIn(['php-fpm8.5', '-t'], plan['validate'])
        self.assertEqual(plan['observations']['fpm']['socket'], '/run/php/php8.5-fpm.sock')
        self.assertIn('/etc/php/8.5/fpm/conf.d/', plan['files'][0]['path'])
        self.write_plan(plan)
        evidence = {'sapi': 'fpm-fcgi', 'values': {**FIELDS['php'], 'short_open_tag': '1',
                    'session.upload_progress.enabled': '', 'expose_php': ''},
                    'scanned': plan['files'][0]['path'], 'extensions': {'pdo_sqlite': True, 'sqlite3': True}}
        with patch('server_setting.root_fpm.probe_values', return_value=evidence):
            self.assertTrue(self.recipes.verify_plan(self.host, plan)['verified'])
        evidence['values']['memory_limit'] = '32M'
        with patch('server_setting.root_fpm.probe_values', return_value=evidence), self.assertRaisesRegex(ValueError, 'memory_limit'):
            self.recipes.verify_plan(self.host, plan)
        evidence['values']['memory_limit'] = '128M'
        evidence['scanned'] = '/etc/php/8.5/fpm/conf.d/20-sqlite3.ini'
        with patch('server_setting.root_fpm.probe_values', return_value=evidence), self.assertRaisesRegex(ValueError, 'loaded|scanned'):
            self.recipes.verify_plan(self.host, plan)

    def test_fpm_bootstrap_config_is_refused_without_touching_existing_content(self):
        cases = [('/etc/php/8.5/fpm/php.ini', '[PHP]\nauto_prepend_file=/srv/bootstrap.php\n'),
                 ('/etc/php/8.5/fpm/conf.d/30-application.ini', 'auto_append_file="/srv/finish.php"\n'),
                 ('/etc/php/8.5/fpm/pool.d/www.conf', '[www]\nuser=www-data\nlisten=/run/php/php8.5-fpm.sock\nphp_admin_value[auto_prepend_file]=/srv/app.php\n'),
                 ('/etc/php/8.5/fpm/pool.d/www.conf', '[www]\nuser=www-data\nlisten=/run/php/php8.5-fpm.sock\nphp_value[auto_append_file]=${APPEND}\n')]
        for path, content in cases:
            with self.subTest(path=path, content=content.splitlines()[-1]):
                self.seed_fpm()
                extra = self.host.path('/etc/php/8.5/fpm/conf.d/30-application.ini')
                if extra.exists():
                    extra.unlink()
                self.host.put(path, content)
                before = self.host.path(path).read_bytes()
                with self.assertRaisesRegex(ValueError, 'bootstrap|prepend|append'):
                    self.recipes.build_plan(self.host, 'php', dict(FIELDS['php']))
                self.assertEqual(self.host.path(path).read_bytes(), before)

    def test_fpm_verify_rechecks_bootstrap_before_sending_a_request(self):
        plan = self.recipes.build_plan(self.host, 'php', dict(FIELDS['php']))
        self.write_plan(plan)
        self.host.put('/etc/php/8.5/fpm/conf.d/99-site.ini', 'auto_prepend_file = /srv/application.php\n')
        with patch('server_setting.root_fpm.probe_values', side_effect=AssertionError('A bootstrap must never execute')):
            with self.assertRaisesRegex(ValueError, 'bootstrap|prepend'):
                self.recipes.verify_plan(self.host, plan)

    def test_fpm_custom_unit_environment_and_exec_are_refused_without_leaking_values(self):
        cases = [{'Environment': 'PHPRC=/secret/SENSITIVE_APP'},
                 {'Environment': 'DATABASE_PASSWORD=SENSITIVE_APP'},
                 {'DropInPaths': '/etc/systemd/system/php8.5-fpm.service.d/custom.conf'},
                 {'EnvironmentFiles': '/etc/default/php8.5-fpm (ignore_errors=yes)'},
                 {'PassEnvironment': 'PHP_INI_SCAN_DIR'}, {'RootDirectory': '/srv/chroot'},
                 {'RootImage': '/srv/chroot.img'},
                 {'ExecStart': '{ path=/usr/sbin/php-fpm8.5 ; argv[]=/usr/sbin/php-fpm8.5 --nodaemonize --fpm-config /opt/custom/php-fpm.conf ; pid=0 ; }'},
                 {'ExecStart': '{ path=/opt/custom/php-fpm8.5 ; argv[]=/opt/custom/php-fpm8.5 --nodaemonize --fpm-config /etc/php/8.5/fpm/php-fpm.conf ; pid=0 ; }'}]
        for overrides in cases:
            with self.subTest(properties=list(overrides)):
                self.host.fpm_unit_overrides = overrides
                with self.assertRaisesRegex(ValueError, 'systemd|unit|environment|ExecStart') as rejected:
                    self.recipes.build_plan(self.host, 'php', dict(FIELDS['php']))
                self.assertNotIn('SENSITIVE_APP', str(rejected.exception))

    def test_native_fpm_empty_systemd_arrays_do_not_look_like_unknown_properties(self):
        plan = self.recipes.build_plan(self.host, 'php', dict(FIELDS['php']))
        self.assertEqual(plan['service_snapshot']['unit'], 'php8.5-fpm.service')

    def test_fpm_rechecks_unit_before_probe_and_rejects_manager_php_environment(self):
        plan = self.recipes.build_plan(self.host, 'php', dict(FIELDS['php']))
        self.write_plan(plan)
        self.host.fpm_unit_overrides = {'Environment': 'PHP_INI_SCAN_DIR=/opt/custom'}
        with patch('server_setting.root_fpm.probe_values', side_effect=AssertionError('Custom FPM must never be probed')):
            with self.assertRaisesRegex(ValueError, 'systemd|unit|environment'):
                self.recipes.verify_plan(self.host, plan)
        self.host.fpm_unit_overrides = {}
        self.host.manager_environment = 'PATH=/usr/bin\nPHP_INI_SCAN_DIR=/secret/SENSITIVE_APP\n'
        with self.assertRaisesRegex(ValueError, 'environment') as rejected:
            self.recipes.build_plan(self.host, 'php', dict(FIELDS['php']))
        self.assertNotIn('SENSITIVE_APP', str(rejected.exception))

    def test_fpm_accepts_empty_bootstrap_values_and_native_module_link(self):
        path = self.host.path('/etc/php/8.5/fpm/conf.d/20-sqlite3.ini')
        self.host.put('/etc/php/8.5/mods-available/sqlite3.ini', '; native module\nextension=sqlite3.so\n')
        self.host.put('/etc/php/8.5/fpm/conf.d/40-empty.ini', "auto_prepend_file = '' ; empty\nauto_append_file=\"\"\n")
        self.host.put('/etc/php/8.5/fpm/conf.d/20-sqlite3.ini', '; link placeholder\n')
        if os.name != 'nt':
            path.unlink()
            path.symlink_to('../../mods-available/sqlite3.ini')
            self.assertEqual(self.recipes.build_plan(self.host, 'php', dict(FIELDS['php']))['module'], 'php')
            return
        original_link = type(path).is_symlink
        original_readlink = type(path).readlink
        original_lstat = type(path).lstat
        link_stat = os.stat_result((stat.S_IFLNK | 0o777, *tuple(path.lstat())[1:]))
        # Windows symlink creation requires elevation; substitute only this OS boundary.
        with patch.object(type(path), 'is_symlink', lambda current: current == path or original_link(current)), \
             patch.object(type(path), 'readlink', lambda current: Path('../../mods-available/sqlite3.ini') if current == path else original_readlink(current)), \
             patch.object(type(path), 'lstat', lambda current: link_stat if current == path else original_lstat(current)):
            plan = self.recipes.build_plan(self.host, 'php', dict(FIELDS['php']))
            self.host.put('/etc/php/8.5/mods-available/sqlite3.ini', 'auto_prepend_file=/srv/native-bootstrap.php\n')
            with self.assertRaisesRegex(ValueError, 'bootstrap|prepend'):
                self.recipes.build_plan(self.host, 'php', dict(FIELDS['php']))
        self.assertEqual(plan['module'], 'php')

    @unittest.skipIf(os.name == 'nt', 'POSIX configuration permissions are checked on Linux')
    def test_fpm_refuses_group_or_world_writable_native_config(self):
        path = self.host.path('/etc/php/8.5/fpm/php.ini')
        before = path.read_bytes()
        path.chmod(0o666)
        with self.assertRaisesRegex(ValueError, 'root controlled|writable'):
            self.recipes.build_plan(self.host, 'php', dict(FIELDS['php']))
        self.assertEqual(path.read_bytes(), before)

    def test_fpm_rejects_unknown_module_link_even_when_target_is_empty(self):
        path = self.host.path('/etc/php/8.5/fpm/conf.d/20-unknown.ini')
        self.host.put('/etc/php/8.5/fpm/conf.d/20-unknown.ini', '; placeholder\n')
        if os.name != 'nt':
            path.unlink()
            path.symlink_to('/opt/custom/site.ini')
            with self.assertRaisesRegex(ValueError, 'symlink|link|target'):
                self.recipes.build_plan(self.host, 'php', dict(FIELDS['php']))
            return
        original_link = type(path).is_symlink
        original_readlink = type(path).readlink
        original_lstat = type(path).lstat
        link_stat = os.stat_result((stat.S_IFLNK | 0o777, *tuple(path.lstat())[1:]))
        with patch.object(type(path), 'is_symlink', lambda current: current == path or original_link(current)), \
             patch.object(type(path), 'readlink', lambda current: Path('/opt/custom/site.ini') if current == path else original_readlink(current)), \
             patch.object(type(path), 'lstat', lambda current: link_stat if current == path else original_lstat(current)):
            with self.assertRaisesRegex(ValueError, 'symlink|link|target'):
                self.recipes.build_plan(self.host, 'php', dict(FIELDS['php']))

    def test_php_multiple_versions_or_pools_refuse_to_guess(self):
        self.seed_fpm('8.4')
        with self.assertRaisesRegex(ValueError, 'Multiple|multiple'):
            self.recipes.build_plan(self.host, 'php', dict(FIELDS['php']))
        self.host.path('/etc/php/8.4/fpm/php.ini').unlink()
        self.host.put('/etc/php/8.5/fpm/pool.d/custom.conf', '[custom]\nlisten=/run/php/custom.sock\n')
        with self.assertRaisesRegex(ValueError, 'pool|Pool'):
            self.recipes.build_plan(self.host, 'php', dict(FIELDS['php']))

    def test_written_managed_recipes_replan_as_noop_without_reload(self):
        for module in ('apache', 'php', 'vsftpd', 'samba', 'mariadb'):
            with self.subTest(module=module):
                first = self.recipes.build_plan(self.host, module, dict(FIELDS[module]))
                self.write_plan(first)
                second = self.recipes.build_plan(self.host, module, dict(FIELDS[module]))
                self.assertEqual([f['content'] for f in second['files']], [f['content'] for f in first['files']])
                self.assertEqual(second['apply_commands'], [])

    def test_missing_running_service_cannot_be_reported_verified(self):
        plan = self.recipes.build_plan(self.host, 'vsftpd', dict(FIELDS['vsftpd']))
        self.write_plan(plan)
        self.host.active = False
        with self.assertRaisesRegex(ValueError, 'active|inactive|stopped'):
            self.recipes.verify_plan(self.host, plan)

    def test_boolean_runtime_garbage_is_not_treated_as_off(self):
        plan = self.recipes.build_plan(self.host, 'mariadb', dict(FIELDS['mariadb']))
        self.host.effective = {'slow_query_log': 'ON', 'long_query_time': '1', 'log_queries_not_using_indexes': 'garbage'}
        with self.assertRaisesRegex(ValueError, 'log_queries_not_using_indexes'):
            self.recipes.verify_plan(self.host, plan)

    def test_tool_owned_path_with_unknown_content_is_not_overwritten(self):
        self.host.put('/etc/php/8.5/fpm/conf.d/zz-3wa-server-setup.ini', '; Administrator custom file\nmemory_limit=24M\n')
        with self.assertRaisesRegex(ValueError, 'ownership|content'):
            self.recipes.build_plan(self.host, 'php', dict(FIELDS['php']))

    def test_unsafe_timezone_name_is_rejected_before_commands(self):
        with self.assertRaises(ValueError):
            self.recipes.build_plan(self.host, 'time', {'Timezone': '../etc/shadow'})
        self.assertEqual(self.host.commands, [])

    def test_noop_timezone_does_not_run_set_timezone(self):
        self.host.timezone = 'Asia/Taipei'
        plan = self.recipes.build_plan(self.host, 'time', dict(FIELDS['time']))
        self.assertEqual(plan['apply_commands'], [])
        self.assertEqual(plan['restore_commands'], [])

    def test_mariadb_does_not_touch_credentials_or_client_config(self):
        self.host.put('/root/.my.cnf', '[client]\npassword=SECRET\n')
        self.host.effective = {'slow_query_log': 'ON', 'long_query_time': '1.000000', 'log_queries_not_using_indexes': 'OFF'}
        plan = self.recipes.build_plan(self.host, 'mariadb', dict(FIELDS['mariadb']))
        self.write_plan(plan)
        self.assertEqual(self.host.read('/etc/mysql/my.cnf'), '[client-server]\n!includedir /etc/mysql/mariadb.conf.d/\n')
        self.assertEqual(plan['validate'], [['mariadbd', '--defaults-file=/etc/mysql/my.cnf', '--verbose', '--help']])
        self.assertTrue(self.recipes.verify_plan(self.host, plan)['verified'])
        command = next(c for c in self.host.commands if c[0] == 'mariadb')
        self.assertEqual(command[1:3], ['--no-defaults', '--protocol=socket'])
        self.assertNotIn('/root/.my.cnf', self.host.reads)
        self.host.effective['long_query_time'] = '9.000000'
        with self.assertRaisesRegex(ValueError, 'long_query_time'):
            self.recipes.verify_plan(self.host, plan)

    def test_time_has_old_zone_for_restore_and_repeated_properties(self):
        plan = self.recipes.build_plan(self.host, 'time', dict(FIELDS['time']))
        self.assertEqual(plan['apply_commands'], [['timedatectl', 'set-timezone', 'Asia/Taipei']])
        self.assertEqual(plan['restore_commands'], [['timedatectl', 'set-timezone', 'Etc/UTC']])
        self.assertEqual(plan['observations']['previous_timezone'], 'Etc/UTC')
        self.host.timezone = 'Asia/Taipei'
        verified = self.recipes.verify_plan(self.host, plan)
        self.assertTrue(verified['verified'])
        self.assertFalse(verified['ntp_synchronized'])
        self.assertTrue(all('--property=Timezone,NTPSynchronized' not in c for c in self.host.commands))

    def test_inactive_service_is_saved_but_unverified(self):
        plan = self.recipes.build_plan(self.host, 'vsftpd', dict(FIELDS['vsftpd']), service_active=False)
        self.write_plan(plan)
        self.host.active = False
        result = self.recipes.verify_plan(self.host, plan)
        self.assertFalse(result['verified'])
        self.assertIn('inactive', result['detail'].lower())

    def test_unknown_partial_or_multiline_targets_are_rejected(self):
        for module, values in [('startup', {}), ('firewall', {}), ('apache', {'ServerTokens': 'Prod'}),
                               ('apache', {**FIELDS['apache'], 'evil': 'x'}),
                               ('php', {**FIELDS['php'], 'date.timezone': 'Asia/Taipei\nevil=1'})]:
            with self.subTest(module=module), self.assertRaises(ValueError):
                self.recipes.build_plan(self.host, module, values)

    def test_post_install_enables_only_distro_fpm_without_legacy_migration(self):
        self.host.put('/etc/apache2/conf-available/php8.5-fpm.conf', '# distro FPM handler\n')
        result = self.recipes.post_install(self.host, 'php')
        self.assertTrue(result['integrated'])
        self.assertIn(['a2enmod', 'proxy_fcgi', 'setenvif'], self.host.commands)
        self.assertIn(['a2enconf', 'php8.5-fpm'], self.host.commands)
        self.host.commands.clear()
        self.host.put('/etc/apache2/mods-enabled/mpm_prefork.load', 'LoadModule mpm_prefork_module /usr/lib/apache2/modules/mod_mpm_prefork.so\n')
        result = self.recipes.post_install(self.host, 'php')
        self.assertFalse(result['integrated'])
        self.assertFalse(any(c[0] in {'a2enmod', 'a2enconf'} for c in self.host.commands))


if __name__ == '__main__':
    unittest.main()

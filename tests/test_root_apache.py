"""Real configuration files and explicit process/symlink OS boundaries."""
from contextlib import contextmanager
import copy
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from server_setting.root_actions import Host, RootActionError, RootManager

try:
    from server_setting.root_apache import ApacheFPMManager
except ImportError:
    ApacheFPMManager = None


@contextmanager
def native_links():
    """Windows lacks link privilege; substitute only filesystem link syscalls."""
    if os.name != 'nt':
        yield
        return
    links = {}
    original_sym = Path.is_symlink
    original_read = Path.readlink
    original_stat = Path.lstat
    original_unlink = Path.unlink
    original_replace = os.replace

    def key(path):
        return os.path.normcase(os.path.abspath(path))

    def symlink(target, link_name, *args, **kwargs):
        Path(link_name).write_bytes(b'')
        links[key(link_name)] = str(target)

    def lstat(path):
        result = original_stat(path)
        if key(path) in links:
            values = list(result)
            values[0] = stat.S_IFLNK | 0o777
            return os.stat_result(values)
        return result

    def unlink(path, missing_ok=False):
        original_unlink(path, missing_ok=missing_ok)
        links.pop(key(path), None)

    def replace(source, target):
        original_replace(source, target)
        prior = links.pop(key(source), None)
        links.pop(key(target), None)
        if prior is not None:
            links[key(target)] = prior

    with patch.object(Path, 'is_symlink', lambda path: key(path) in links or original_sym(path)), \
            patch.object(Path, 'readlink', lambda path: Path(links[key(path)]) if key(path) in links else original_read(path)), \
            patch.object(Path, 'lstat', lstat), patch.object(Path, 'unlink', unlink), \
            patch('os.symlink', symlink), patch('os.replace', replace):
        yield


class ApacheHandlerTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(ApacheFPMManager, 'Explicit Apache event/FPM migration is missing')
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.link_context = native_links()
        self.link_context.__enter__()
        self.addCleanup(self.link_context.__exit__, None, None, None)
        self.calls = []
        self.fail_command = None
        self.reject_event_config = False
        self.services = {'apache2.service': {'active': True, 'enabled': True},
                         'php8.5-fpm.service': {'active': False, 'enabled': False}}
        outer = self

        class ApacheHost(Host):
            def run(self, argv, timeout=30):
                outer.calls.append(list(argv))
                if list(argv) == outer.fail_command:
                    outer.fail_command = None
                    return subprocess.CompletedProcess(argv, 1, 'private-output', 'private-error')
                if (outer.reject_event_config and argv in (['apache2ctl', 'configtest'], ['apache2ctl', '-M'])
                        and (outer.root / 'etc/apache2/mods-enabled/mpm_event.load').is_symlink()):
                    return subprocess.CompletedProcess(argv, 1, '', 'event configuration rejected')
                out = ''
                if argv[0] == 'dpkg-query':
                    out = 'install ok installed\n'
                elif argv[:2] == ['systemctl', 'show-environment']:
                    pass
                elif argv[:2] == ['systemctl', 'show']:
                    unit = argv[2]
                    if '--property=ExecStart' in argv:
                        binary = '/usr/sbin/apachectl' if unit == 'apache2.service' else '/usr/sbin/php-fpm8.5'
                        args = binary + (' start' if unit == 'apache2.service' else ' --nodaemonize --fpm-config /etc/php/8.5/fpm/php-fpm.conf')
                        out = ('DropInPaths=\nEnvironmentFiles=\nEnvironment=\nPassEnvironment=\nRootDirectory=\nRootImage=\n'
                               'ExecStart={ path=' + binary + ' ; argv[]=' + args + ' ; }\n')
                    else:
                        item = outer.services[unit]
                        out = ('LoadState=loaded\nActiveState=' + ('active' if item['active'] else 'inactive')
                               + '\nUnitFileState=' + ('enabled' if item['enabled'] else 'disabled') + '\n')
                elif argv[:1] == ['systemctl']:
                    operation, unit = argv[1:3]
                    if operation in {'enable', 'disable'}:
                        outer.services[unit]['enabled'] = operation == 'enable'
                    elif operation in {'start', 'restart', 'stop'}:
                        outer.services[unit]['active'] = operation != 'stop'
                elif argv == ['apache2ctl', '-M']:
                    modules = ['core_module']
                    for path in sorted((outer.root / 'etc/apache2/mods-enabled').glob('*.load')):
                        name = path.name[:-5]
                        modules.append('php_module' if name.startswith('php') else name + '_module')
                    out = '\n'.join(name + ' (shared)' for name in modules)
                elif argv == ['apache2ctl', '-t', '-D', 'DUMP_INCLUDES']:
                    out = ('Included configuration files:\n  (*) /etc/apache2/apache2.conf\n'
                           '    (1) /etc/apache2/sites-enabled/000-default.conf\n')
                return subprocess.CompletedProcess(argv, 0, out, '')

        self.host = ApacheHost(self.root)
        self.core = RootManager(self.host)
        @contextmanager
        def unlocked():
            yield
        patcher = patch.object(self.core, '_lock', unlocked)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch('server_setting.root_actions.os.geteuid', return_value=0, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.target = {'version': '8.5', 'unit': 'php8.5-fpm.service', 'socket': '/run/php/php8.5-fpm.sock',
                       'pool': 'www', 'ini': '/etc/php/8.5/fpm/conf.d/zz-3wa-server-setup.ini', 'binary': 'php-fpm8.5'}
        patcher = patch('server_setting.root_recipes._fpm', return_value=self.target)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch('server_setting.root_fpm.probe_values', return_value={'sapi': 'fpm-fcgi', 'values': {}})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.put('/etc/os-release', 'ID=ubuntu\nVERSION_ID="26.04"\n')
        self.put('/etc/apache2/apache2.conf', 'IncludeOptional mods-enabled/*.load\nIncludeOptional mods-enabled/*.conf\nInclude ports.conf\nIncludeOptional conf-enabled/*.conf\nIncludeOptional sites-enabled/*.conf\n')
        self.put('/etc/apache2/ports.conf', 'Listen 80\n')
        self.put('/etc/apache2/sites-available/000-default.conf', '<VirtualHost *:80>\nDocumentRoot /var/www/html\n</VirtualHost>\n')
        self.put('/etc/apache2/conf-available/php8.5-fpm.conf', '<IfModule proxy_fcgi_module>\n<FilesMatch "\\.php$">\nSetHandler "proxy:unix:/run/php/php8.5-fpm.sock|fcgi://localhost"\n</FilesMatch>\n</IfModule>\n')
        for name, depends in (('authz_core', ''), ('slotmem_shm', ''), ('mpm_prefork', 'authz_core'),
                              ('mpm_worker', 'authz_core'), ('mpm_event', 'authz_core'),
                              ('proxy', 'slotmem_shm'), ('proxy_fcgi', 'proxy'), ('setenvif', '')):
            self.put('/etc/apache2/mods-available/' + name + '.load',
                     ('# Depends: ' + depends + '\n' if depends else '')
                     + 'LoadModule ' + name + '_module /usr/lib/apache2/modules/mod_' + name + '.so\n')
            self.put('/usr/lib/apache2/modules/mod_' + name + '.so', 'native module fixture')
        for name in ('mpm_prefork', 'mpm_worker', 'mpm_event', 'proxy'):
            self.put('/etc/apache2/mods-available/' + name + '.conf', '# native module settings\n')
        self.put('/etc/apache2/mods-available/php8.5.load', 'LoadModule php_module /usr/lib/apache2/modules/libphp8.5.so\n')
        self.put('/usr/lib/apache2/modules/libphp8.5.so', 'native PHP fixture')
        self.put('/etc/apache2/mods-available/php8.5.conf', '<FilesMatch "\\.php$">\nSetHandler application/x-httpd-php\n</FilesMatch>\n')
        self.put('/etc/php/8.5/fpm/php.ini', '; existing php configuration\n')
        self.put('/etc/php/8.5/fpm/php-fpm.conf', 'include=/etc/php/8.5/fpm/pool.d/*.conf\n')
        self.put('/etc/php/8.5/fpm/pool.d/www.conf', '[www]\nuser=www-data\nlisten=/run/php/php8.5-fpm.sock\n')
        (self.root / 'etc/php/8.5/fpm/conf.d').mkdir()
        (self.root / 'etc/php/8.5/mods-available').mkdir()
        for name in ('mods-enabled', 'conf-enabled', 'sites-enabled'):
            (self.root / 'etc/apache2' / name).mkdir()
        self.link('mods-enabled/mpm_prefork.load', '../mods-available/mpm_prefork.load')
        self.link('mods-enabled/mpm_prefork.conf', '../mods-available/mpm_prefork.conf')
        self.link('mods-enabled/php8.5.load', '../mods-available/php8.5.load')
        self.link('mods-enabled/php8.5.conf', '../mods-available/php8.5.conf')
        self.link('sites-enabled/000-default.conf', '../sites-available/000-default.conf')
        self.manager = ApacheFPMManager(self.core)

    def put(self, virtual, text):
        path = self.root / virtual.lstrip('/')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def link(self, suffix, target):
        (self.root / 'etc/apache2' / suffix).symlink_to(target)

    def aliases(self):
        return {path.relative_to(self.root).as_posix(): path.readlink().as_posix()
                for path in (self.root / 'etc/apache2').rglob('*') if path.is_symlink()}

    def test_plan_is_read_only_and_describes_full_handler_change(self):
        before = self.aliases()
        plan = self.manager.plan()
        self.assertEqual(plan['module'], 'apache-fpm')
        self.assertEqual(plan['target']['version'], '8.5')
        self.assertEqual(self.aliases(), before)
        self.assertFalse((self.root / 'var').exists())
        targets = {item['path']: item['after'] for item in plan['links']}
        self.assertIsNone(targets['/etc/apache2/mods-enabled/php8.5.load'])
        self.assertEqual(targets['/etc/apache2/mods-enabled/mpm_event.load'], '../mods-available/mpm_event.load')
        self.assertEqual(targets['/etc/apache2/conf-enabled/php8.5-fpm.conf'], '../conf-available/php8.5-fpm.conf')
        self.assertFalse(any(call[:2] == ['systemctl', 'restart'] for call in self.calls))

    def test_apply_starts_fpm_before_apache_restart_and_preserves_unrelated_aliases(self):
        before_site = self.aliases()['etc/apache2/sites-enabled/000-default.conf']
        result = self.manager.apply(self.manager.plan())
        self.assertEqual(result['status'], 'APACHE_FPM_APPLIED')
        self.assertTrue(result['verification']['event_module'])
        self.assertTrue(result['verification']['fpm_request'])
        self.assertFalse(result['verification']['http_application'])
        self.assertLess(self.calls.index(['systemctl', 'start', 'php8.5-fpm.service']),
                        self.calls.index(['systemctl', 'restart', 'apache2.service']))
        self.assertNotIn(['systemctl', 'reload', 'apache2.service'], self.calls)
        self.assertTrue((self.root / 'etc/apache2/mods-enabled/mpm_event.load').is_symlink())
        self.assertFalse((self.root / 'etc/apache2/mods-enabled/php8.5.load').exists())
        self.assertEqual((self.root / 'etc/apache2/sites-enabled/000-default.conf').readlink().as_posix(), before_site)

    def test_stale_vhost_plan_and_forged_rehashed_target_never_write(self):
        plan = self.manager.plan()
        original = self.aliases()
        self.put('/etc/apache2/sites-available/000-default.conf', '# external vhost edit\n')
        with self.assertRaises(RootActionError):
            self.manager.apply(plan)
        self.assertEqual(self.aliases(), original)
        from server_setting.root_actions import _fingerprint
        forged = self.manager.plan()
        forged['links'][0]['after'] = '/tmp/untrusted-handler.conf'
        forged['fingerprint'] = _fingerprint(forged)
        with self.assertRaises(RootActionError):
            self.manager.apply(forged)
        self.assertEqual(self.aliases(), original)
        self.assertFalse((self.root / 'var').exists())

    def test_restart_failure_restores_every_alias_and_both_service_states(self):
        before_links, before_services = self.aliases(), copy.deepcopy(self.services)
        plan = self.manager.plan()
        self.fail_command = ['systemctl', 'restart', 'apache2.service']
        with self.assertRaises(RootActionError) as raised:
            self.manager.apply(plan)
        self.assertNotIn('private', str(raised.exception))
        self.assertEqual(self.aliases(), before_links)
        self.assertEqual(self.services, before_services)
        self.assertEqual(self.core._journals()[-1]['status'], 'APACHE_FPM_ROLLED_BACK')

    def test_invalid_candidate_configuration_can_roll_back_without_loading_it(self):
        before_links, before_services = self.aliases(), copy.deepcopy(self.services)
        plan = self.manager.plan()
        self.reject_event_config = True
        with self.assertRaises(RootActionError) as raised:
            self.manager.apply(plan)
        self.assertEqual(raised.exception.code, 'APACHE_FPM_ROLLED_BACK')
        self.assertEqual(self.aliases(), before_links)
        self.assertEqual(self.services, before_services)
        self.assertEqual(self.core._journals()[-1]['status'], 'APACHE_FPM_ROLLED_BACK')

    def test_fpm_validation_failure_leaves_live_apache_running_without_restart(self):
        before = self.aliases()
        plan = self.manager.plan()
        self.fail_command = ['php-fpm8.5', '-t']
        with self.assertRaises(RootActionError) as raised:
            self.manager.apply(plan)
        self.assertEqual(raised.exception.code, 'APACHE_FPM_ROLLED_BACK')
        self.assertEqual(self.aliases(), before)
        self.assertNotIn(['systemctl', 'restart', 'apache2.service'], self.calls)

    def test_alias_fsync_failure_after_atomic_replace_still_restores_the_changed_alias(self):
        before = self.aliases()
        plan = self.manager.plan()
        original_sync = self.manager._sync_directory
        first = True

        def fail_once(directory):
            nonlocal first
            if first:
                first = False
                raise OSError('fixture fsync failure after alias replacement')
            original_sync(directory)

        with patch.object(self.manager, '_sync_directory', side_effect=fail_once):
            with self.assertRaises(RootActionError) as raised:
                self.manager.apply(plan)
        self.assertEqual(raised.exception.code, 'APACHE_FPM_ROLLED_BACK')
        self.assertEqual(self.aliases(), before)
        self.assertEqual(self.core._journals()[-1]['status'], 'APACHE_FPM_ROLLED_BACK')

    def test_reviewed_restore_returns_original_inactive_apache_state(self):
        self.services['apache2.service']['active'] = False
        before = self.aliases()
        self.manager.apply(self.manager.plan())
        restore = self.manager.restore_plan()
        php_restore = next(entry for entry in restore['links']
                           if entry['path'] == '/etc/apache2/mods-enabled/php8.5.load')
        self.assertIsNone(php_restore['before'])
        self.assertEqual(php_restore['after'], '../mods-available/php8.5.load')
        result = self.manager.restore(restore)
        self.assertEqual(result['status'], 'APACHE_FPM_RESTORED')
        self.assertEqual(self.aliases(), before)
        self.assertFalse(self.services['apache2.service']['active'])
        self.assertFalse(self.services['php8.5-fpm.service']['active'])
        self.assertFalse(self.services['php8.5-fpm.service']['enabled'])

    def test_repeated_apply_does_not_restart_or_hide_the_original_handler_backup(self):
        original = self.aliases()
        first = self.manager.apply(self.manager.plan())
        journal_count = len(self.core._journals())
        restart_count = self.calls.count(['systemctl', 'restart', 'apache2.service'])
        result = self.manager.apply(self.manager.plan())
        self.assertEqual(result['status'], 'NO_CHANGE')
        self.assertEqual(len(self.core._journals()), journal_count)
        self.assertEqual(self.calls.count(['systemctl', 'restart', 'apache2.service']), restart_count)
        restore = self.manager.restore_plan()
        self.assertEqual(restore['transaction_id'], first['transaction_id'])
        self.manager.restore(restore)
        self.assertEqual(self.aliases(), original)

    def test_restore_refuses_external_vhost_change(self):
        self.manager.apply(self.manager.plan())
        plan = self.manager.restore_plan()
        after_links = self.aliases()
        self.put('/etc/apache2/sites-available/000-default.conf', '# changed after handler migration\n')
        with self.assertRaises(RootActionError):
            self.manager.restore(plan)
        self.assertEqual(self.aliases(), after_links)

    def test_untrusted_enabled_alias_and_custom_php_handler_are_refused(self):
        enabled = self.root / 'etc/apache2/mods-enabled/php8.5.load'
        enabled.unlink()
        enabled.symlink_to('/tmp/php.load')
        with self.assertRaises(RootActionError):
            self.manager.plan()
        enabled.unlink()
        enabled.symlink_to('../mods-available/php8.5.load')
        self.put('/etc/apache2/sites-available/000-default.conf', 'SetHandler "proxy:unix:/tmp/custom.sock|fcgi://localhost"\n')
        with self.assertRaises(RootActionError):
            self.manager.plan()

    def test_direct_php_or_mpm_loader_in_a_vhost_is_not_a_native_alias_layout(self):
        self.put('/etc/apache2/sites-available/000-default.conf',
                 'LoadModule mpm_prefork_module /usr/lib/apache2/modules/mod_mpm_prefork.so\n')
        with self.assertRaises(RootActionError):
            self.manager.plan()

    def test_native_loadmodule_or_unsupported_dependency_change_is_refused(self):
        path = '/etc/apache2/mods-available/proxy_fcgi.load'
        self.put(path, 'LoadModule proxy_fcgi_module /tmp/custom.so\n')
        with self.assertRaises(RootActionError):
            self.manager.plan()
        self.put(path, '# Depends: proxy\nLoadModule proxy_fcgi_module /usr/lib/apache2/modules/mod_proxy_fcgi.so\n')
        self.put('/etc/apache2/mods-available/php8.5.load', 'LoadModule php_module /tmp/alternative-php.so\n')
        with self.assertRaises(RootActionError):
            self.manager.plan()
        self.put('/etc/apache2/mods-available/php8.5.load', 'LoadModule php_module /usr/lib/apache2/modules/libphp8.5.so\n')
        self.put(path, '# Depends: custom_module\nLoadModule proxy_fcgi_module /usr/lib/apache2/modules/mod_proxy_fcgi.so\n')
        with self.assertRaises(RootActionError):
            self.manager.plan()

"""Explicit, reversible native Apache MPM event and PHP-FPM activation."""
import hashlib
import os
from pathlib import Path
import re
import stat
import uuid

from . import root_recipes
from .root_actions import RootActionError, RootManager, _fingerprint


MODULE = 'apache-fpm'
KIND = 'apache-fpm'
APACHE = 'apache2.service'
_DEPENDENCIES = frozenset({'mpm_event', 'proxy', 'proxy_fcgi', 'setenvif', 'authz_core', 'slotmem_shm'})
_NAME = r'[A-Za-z0-9_.-]{1,128}'


class ApacheFPMManager:
    def __init__(self, root_manager=None, host=None):
        self.core = root_manager or RootManager(host)
        self.host = self.core.host

    def _error(self, error):
        if isinstance(error, RootActionError):
            return error
        return RootActionError(str(error) if isinstance(error, ValueError) else
                               'Apache handler operation failed; inspect its private transaction journal.',
                               'APACHE_FPM_FAILED')

    def _directory(self, virtual):
        path = self.host.path(virtual)
        for item in [path, *path.parents]:
            info = item.lstat()
            if (not stat.S_ISDIR(info.st_mode) or os.name == 'posix'
                    and (info.st_uid != 0 or info.st_mode & 0o022)):
                raise ValueError('Apache/PHP configuration directories must be root controlled.')
            if item == self.host.root:
                break
        return path

    def _file(self, virtual, limit=1024 * 1024):
        path = self.host.path(virtual)
        self.core._trusted_target(path)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('Native Apache/PHP input is missing, nonregular or too large.')
        return path.read_bytes()

    def _alias(self, virtual):
        """Allow only Ubuntu's fixed enabled-to-available read alias chains."""
        parent, name = virtual.rsplit('/', 1)
        path = self._directory(parent) / name
        if not path.is_symlink():
            if path.exists():
                raise ValueError('A managed Apache enabled entry is not a native symlink.')
            return None
        info = path.lstat()
        if os.name == 'posix' and (info.st_uid != 0 or info.st_gid != 0):
            raise ValueError('Apache/PHP native aliases must be root owned.')
        target = path.readlink().as_posix()
        match = re.fullmatch(r'/etc/apache2/(mods|conf|sites)-enabled/(' + _NAME + ')', virtual)
        if match:
            destination = f'/etc/apache2/{match[1]}-available/{match[2]}'
            relative = f'../{match[1]}-available/{match[2]}'
            if target not in {destination, relative}:
                raise ValueError('An Apache enabled alias has an unsupported target.')
        else:
            match = re.fullmatch(r'/etc/php/([0-9]+\.[0-9]+)/fpm/conf.d/(' + _NAME + r'\.ini)', virtual)
            if not match:
                raise ValueError('Unsupported configuration symlink outside native enabled directories.')
            native = re.fullmatch(r'(?:\.\./\.\./mods-available/|/etc/php/'
                                  + re.escape(match[1]) + r'/mods-available/)(' + _NAME + r'\.ini)', target)
            if not native:
                raise ValueError('A PHP FPM alias has an unsupported same-version target.')
            destination = f'/etc/php/{match[1]}/mods-available/{native[1]}'
        self._file(destination)
        if hasattr(os, 'listxattr') and os.listxattr(path, follow_symlinks=False):
            raise ValueError('Extended attributes on native aliases need separate integration.')
        return target

    def _inventory(self, base):
        directory = self._directory(base)
        rows, total = [], 0
        for path in sorted(directory.rglob('*')):
            if len(rows) >= 4096:
                raise ValueError('Apache/PHP configuration exceeds the bounded inventory.')
            virtual = base + '/' + path.relative_to(directory).as_posix()
            if path.is_symlink():
                rows.append({'path': virtual, 'alias': self._alias(virtual)})
            elif path.is_dir():
                self._directory(virtual)
            else:
                data = self._file(virtual)
                total += len(data)
                if total > 16 * 1024 * 1024:
                    raise ValueError('Apache/PHP configuration exceeds the bounded inventory.')
                info = path.lstat()
                rows.append({'path': virtual, 'sha256': hashlib.sha256(data).hexdigest(),
                             'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid})
        return rows

    def _service(self, unit):
        result = root_recipes._service(self.host, unit)
        if result['load_state'] != 'loaded' or result['unit_file_state'] not in {'enabled', 'disabled'}:
            raise ValueError('Apache/FPM migration requires loaded, normally enabled or disabled native services.')
        return result

    def _apache_unit(self):
        properties = ('DropInPaths', 'EnvironmentFiles', 'Environment', 'PassEnvironment',
                      'RootDirectory', 'RootImage', 'ExecStart')
        result = self.core._checked(['systemctl', 'show', APACHE, '--no-pager', '--all',
                                     *['--property=' + key for key in properties]])
        observed = root_recipes._properties(result.stdout or '')
        observed.setdefault('EnvironmentFiles', '')
        if (not set(properties).issubset(observed)
                or any(observed[key] for key in properties if key not in {'Environment', 'ExecStart'})
                or observed['Environment'] not in {'', 'APACHE_STARTED_BY_SYSTEMD=true'}):
            raise ValueError('Custom Apache unit environment or filesystem layout needs separate integration.')
        commands = re.findall(r'argv\[\]=(.*?)\s*;', observed['ExecStart'])
        binaries = re.findall(r'(?:^|[;{\s])path=([^;\s]+)\s*;', observed['ExecStart'])
        allowed = {'/usr/sbin/apachectl', '/usr/sbin/apache2ctl'}
        if len(binaries) != 1 or binaries[0] not in allowed or commands != [binaries[0] + ' start']:
            raise ValueError('Custom Apache ExecStart needs separate integration.')
        environment = self.core._checked(['systemctl', 'show-environment']).stdout or ''
        if re.search(r'^(?:APACHE_[A-Z0-9_]+|HTTPD|PHPRC|PHP_INI_SCAN_DIR)=', environment, re.M):
            raise ValueError('Apache/PHP systemd manager overrides need separate integration.')

    def _modules(self):
        output = self.core._checked(['apache2ctl', '-M']).stdout or ''
        modules = re.findall(r'^\s*([A-Za-z0-9_]+_module)\s+\((?:shared|static)\)\s*$', output, re.M)
        if not modules or len(modules) != len(set(modules)):
            raise ValueError('Apache did not return an unambiguous loaded module list.')
        mpms = {name for name in modules if name.startswith('mpm_')}
        if len(mpms) != 1 or not mpms.issubset({'mpm_prefork_module', 'mpm_worker_module', 'mpm_event_module'}):
            raise ValueError('An unsupported or ambiguous Apache MPM needs separate integration.')
        return sorted(modules)

    def _layout(self, target, inventory):
        main = self._file('/etc/apache2/apache2.conf').decode('utf-8')
        for include in ('mods-enabled/*.load', 'mods-enabled/*.conf', 'conf-enabled/*.conf', 'sites-enabled/*.conf'):
            if not re.search(r'^\s*IncludeOptional\s+["\']?(?:/etc/apache2/)?'
                             + re.escape(include) + r'["\']?\s*(?:#.*)?$', main, re.M):
                raise ValueError('Unsupported Apache native enabled-directory include layout.')
        if re.search(r'^\s*ServerRoot\s+(?!["\']?/etc/apache2["\']?\s*(?:#.*)?$)', main, re.M):
            raise ValueError('A nonnative Apache ServerRoot needs separate integration.')
        included = self.core._checked(['apache2ctl', '-t', '-D', 'DUMP_INCLUDES']).stdout or ''
        paths = re.findall(r'^\s*\([^)]*\)\s+(/[^\r\n]+?)\s*$', included, re.M)
        known = {entry['path'] for entry in inventory}
        if '/etc/apache2/apache2.conf' not in paths or any(path not in known for path in paths):
            raise ValueError('Apache includes outside the inventoried native configuration need separate integration.')
        native_php = {'/etc/apache2/mods-available/php' + target['version'] + '.conf',
                      '/etc/apache2/conf-available/php' + target['version'] + '-fpm.conf'}
        native_loaders = {'/etc/apache2/mods-available/' + name + '.load'
                          for name in ('mpm_event', 'mpm_prefork', 'mpm_worker', 'php' + target['version'])}
        for entry in inventory:
            path = entry['path']
            if 'sha256' not in entry or not (path.endswith(('.conf', '.load')) or path in paths):
                continue
            if re.search(r'/php[0-9]+\.[0-9]+(?:\.load|\.conf|-fpm\.conf)$', path) and not any(
                    path.endswith('/php' + target['version'] + suffix) for suffix in ('.load', '.conf', '-fpm.conf')):
                raise ValueError('Multiple Apache PHP handler versions need separate integration.')
            if path in native_php:
                continue
            text = self._file(path).decode('utf-8')
            uncommented = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('#'))
            if (path not in native_loaders and re.search(r'^\s*LoadModule\s+(?:php[0-9]*|mpm_[A-Za-z0-9_]+)_module\b', uncommented, re.M | re.I)):
                raise ValueError('Apache PHP/MPM loaders outside native module aliases need separate integration.')
            if re.search(r'^\s*(?:PHPINIDir\b|(?:SetHandler|AddHandler|Action|ProxyPass(?:Match)?)\b.*(?:php|fcgi))', uncommented, re.M | re.I):
                raise ValueError('Custom Apache PHP routing needs separate integration.')
        handler = self._file('/etc/apache2/conf-available/php' + target['version'] + '-fpm.conf').decode('utf-8')
        handlers = re.findall(r'^\s*SetHandler\s+"?(proxy:unix:[^"\s]+)"?\s*$', handler, re.M)
        if handlers != ['proxy:unix:' + target['socket'] + '|fcgi://localhost']:
            raise ValueError('Native Apache FPM configuration does not point to the selected same-version socket.')

    def _module_input(self, name):
        path = '/etc/apache2/mods-available/' + name + '.load'
        text = self._file(path).decode('utf-8')
        declarations = re.findall(r'^\s*LoadModule\s+(\S+)\s+(\S+)\s*$', text, re.M)
        library = '/usr/lib/apache2/modules/mod_' + name + '.so'
        if declarations != [(name + '_module', library)]:
            raise ValueError('A native Apache LoadModule declaration changed; integration is refused.')
        data = self._file(library, 16 * 1024 * 1024)
        depends = [item for line in re.findall(r'^\s*#\s*Depends:\s*(.*?)\s*$', text, re.M)
                   for item in line.split()]
        if not set(depends).issubset(_DEPENDENCIES):
            raise ValueError('An unsupported Apache module dependency needs separate integration.')
        return depends, {'path': library, 'sha256': hashlib.sha256(data).hexdigest()}

    def _links(self, target):
        desired, libraries, visited = {}, {}, set()

        def enable(name):
            if name in visited:
                return
            visited.add(name)
            depends, library = self._module_input(name)
            libraries[library['path']] = library
            for dependency in depends:
                enable(dependency)
            for suffix in ('.load', '.conf'):
                available = '/etc/apache2/mods-available/' + name + suffix
                if suffix == '.load' or self.host.path(available).exists():
                    self._file(available)
                    desired['/etc/apache2/mods-enabled/' + name + suffix] = '../mods-available/' + name + suffix

        for name in ('mpm_event', 'proxy', 'proxy_fcgi', 'setenvif'):
            enable(name)
        for name in ('mpm_prefork', 'mpm_worker'):
            if self.host.path('/etc/apache2/mods-available/' + name + '.load').exists():
                _, library = self._module_input(name)
                libraries[library['path']] = library
        php_load = '/etc/apache2/mods-available/php' + target['version'] + '.load'
        if self.host.path(php_load).exists():
            declarations = re.findall(r'^\s*LoadModule\s+(\S+)\s+(\S+)\s*$', self._file(php_load).decode('utf-8'), re.M)
            library = '/usr/lib/apache2/modules/libphp' + target['version'] + '.so'
            if declarations != [('php_module', library)]:
                raise ValueError('A custom mod_php LoadModule declaration needs separate integration.')
            libraries[library] = {'path': library, 'sha256': hashlib.sha256(self._file(library, 16 * 1024 * 1024)).hexdigest()}
        for name in ('mpm_prefork', 'mpm_worker', 'php' + target['version']):
            for suffix in ('.load', '.conf'):
                desired['/etc/apache2/mods-enabled/' + name + suffix] = None
        desired['/etc/apache2/conf-enabled/php' + target['version'] + '-fpm.conf'] = '../conf-available/php' + target['version'] + '-fpm.conf'
        links = [{'path': path, 'before': self._alias(path), 'after': desired[path]} for path in sorted(desired)]
        return links, sorted(libraries.values(), key=lambda item: item['path'])

    def _snapshot(self, inspect_apache=True):
        target = root_recipes._fpm(self.host)
        self._apache_unit()
        apache = self._inventory('/etc/apache2')
        if inspect_apache:
            self._layout(target, apache)
        links, libraries = self._links(target)
        return {'target': target, 'apache': apache,
                'fpm': self._inventory('/etc/php/' + target['version'] + '/fpm'),
                'fpm_modules': self._inventory('/etc/php/' + target['version'] + '/mods-available'),
                'libraries': libraries, 'links': links,
                'services': {'apache': self._service(APACHE), 'fpm': self._service(target['unit'])},
                'loaded_modules': self._modules() if inspect_apache else None}

    def status(self):
        try:
            self.core._gate('apache')
            state = self._snapshot()
            return {'supported': True, 'target': state['target'], 'services': state['services'],
                    'event_enabled': 'mpm_event_module' in state['loaded_modules'],
                    'mod_php_enabled': any(name.startswith('php') for name in state['loaded_modules']),
                    'detail': 'Native Apache module configuration observed; HTTP application behavior is unverified.'}
        except Exception as error:
            return {'supported': False, 'detail': str(self._error(error))}

    def plan(self):
        try:
            self.core._gate('apache')
            state = self._snapshot()
            plan = {'schema': 1, 'module': MODULE, 'action': 'apply', 'target': state['target'],
                    'links': state['links'], 'observations': state,
                    'notes': ['Disable native mod_php and prefork/worker aliases; enable event, proxy, proxy_fcgi, setenvif and declared native dependencies.',
                              'Enable and start the selected PHP-FPM service, then restart Apache; existing HTTP connections may be interrupted.',
                              'Apache boot enablement is preserved. All other aliases, vhosts and PHP ini files remain unchanged.',
                              'Verification covers event module configuration, daemon state and a local FPM FastCGI request. HTTP application routing remains unverified.',
                              'Only the supported single-version native Apache/FPM layout is accepted; custom handlers and pools need separate integration.']}
            plan['fingerprint'] = _fingerprint(plan)
            return plan
        except Exception as error:
            raise self._error(error) from None

    def _sync_directory(self, path):
        if os.name == 'posix':
            descriptor = os.open(path, os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _replace_alias(self, path, expected, desired):
        if self._alias(path) != expected:
            raise RootActionError('An Apache alias changed after review; no overwrite was made.', 'EXTERNAL_EDIT')
        if desired == expected:
            return
        parent, name = path.rsplit('/', 1)
        directory = self._directory(parent)
        destination = directory / name
        if desired is None:
            destination.unlink()
            self._sync_directory(directory)
            return
        source = path.replace('-enabled/', '-available/')
        if desired not in {'../' + parent.rsplit('/', 1)[1].replace('-enabled', '-available') + '/' + name, source}:
            raise RootActionError('An Apache alias target is outside the fixed native chain.', 'UNSAFE_PATH')
        self._file(source)
        temporary = directory / ('.3wa-' + uuid.uuid4().hex)
        try:
            os.symlink(desired, temporary)
            if os.name == 'posix':
                os.lchown(temporary, 0, 0)
            if self._alias(path) != expected:
                raise RootActionError('An Apache alias changed while preparing replacement.', 'EXTERNAL_EDIT')
            os.replace(temporary, destination)
            self._sync_directory(directory)
        finally:
            if temporary.is_symlink():
                temporary.unlink()

    def _service_state(self, before, active, enabled, restart=False):
        unit = before['unit']
        now = self._service(unit)
        if now['enabled'] != enabled:
            self.core._checked(['systemctl', 'enable' if enabled else 'disable', unit])
        if active:
            if restart:
                self.core._checked(['systemctl', 'restart', unit], timeout=60)
            elif not now['active']:
                self.core._checked(['systemctl', 'start', unit], timeout=60)
        elif now['active']:
            self.core._checked(['systemctl', 'stop', unit], timeout=60)

    def _restore_services(self, services, restart_apache):
        fpm, apache = services['fpm'], services['apache']
        self._service_state(fpm, fpm['active'], fpm['enabled'])
        self._service_state(apache, apache['active'], apache['enabled'], restart=apache['active'] and restart_apache)

    @staticmethod
    def _unmanaged(snapshot, links):
        paths = {entry['path'] for entry in links}
        return {key: value for key, value in snapshot.items() if key not in {'links', 'loaded_modules', 'services', 'apache'}} | {
            'apache': [entry for entry in snapshot['apache'] if entry['path'] not in paths]}

    def _verify(self, target):
        modules = self._modules()
        if ('mpm_event_module' not in modules
                or any(name.startswith('php') or name in {'mpm_prefork_module', 'mpm_worker_module'} for name in modules)
                or not {'proxy_module', 'proxy_fcgi_module', 'setenvif_module'}.issubset(modules)):
            raise ValueError('Apache configuration did not activate event/FPM without mod_php.')
        from .root_fpm import probe_values
        evidence = probe_values(self.host, target['socket'])
        if evidence.get('sapi') != 'fpm-fcgi':
            raise ValueError('The selected FPM socket did not return a native PHP-FPM request.')
        services = {'apache': self._service(APACHE), 'fpm': self._service(target['unit'])}
        if not all(item['active'] for item in services.values()) or not services['fpm']['enabled']:
            raise ValueError('Apache/FPM service state differs from the reviewed target.')
        return {'event_module': True, 'fpm_request': True, 'http_application': False,
                'detail': 'Native event module configuration, active daemons and local FPM request verified; HTTP application routing unverified.'}

    def _unwind(self, journal):
        # 候選設定可能連 configtest/-M 都不能載入；回復前僅讀輸入與服務狀態。
        current = self._snapshot(inspect_apache=False)
        if self._unmanaged(current, journal['links']) != self._unmanaged(journal['before'], journal['links']):
            raise RootActionError('External configuration changes prevent safe Apache rollback.', 'EXTERNAL_EDIT')
        restored_alias = False
        for entry in reversed(journal['written']):
            current_alias = self._alias(entry['path'])
            if current_alias == entry['before']:
                continue
            if current_alias != entry['after']:
                raise RootActionError('An external Apache alias edit blocks safe rollback.', 'EXTERNAL_EDIT')
            self._replace_alias(entry['path'], entry['after'], entry['before'])
            restored_alias = True
        self.core._checked(['apache2ctl', 'configtest'])
        self._restore_services(journal['before']['services'], restart_apache=restored_alias)
        restored = self._snapshot()
        if restored != journal['before']:
            raise RootActionError('Apache handler restoration did not match its complete snapshot.', 'RECOVERY_REQUIRED')

    def apply(self, preview):
        try:
            self.core._gate('apache')
            with self.core._lock():
                self.core._check_pending()
                if not isinstance(preview, dict) or _fingerprint(preview) != preview.get('fingerprint'):
                    raise RootActionError('An unchanged reviewed Apache handler plan is required.', 'TAMPERED_PLAN')
                plan = self.plan()
                if plan['fingerprint'] != preview['fingerprint']:
                    raise RootActionError('Apache/PHP state changed after preview.', 'STALE_PLAN')
                services = plan['observations']['services']
                if (all(entry['before'] == entry['after'] for entry in plan['links'])
                        and services['apache']['active'] and services['fpm']['active'] and services['fpm']['enabled']):
                    self.core._checked([plan['target']['binary'], '-t'])
                    self.core._checked(['apache2ctl', 'configtest'])
                    return {'status': 'NO_CHANGE', 'module': MODULE,
                            'message': 'Native event/FPM already matches the selected target; previous handler backup is preserved.',
                            'verification': self._verify(plan['target'])}
                self.core._preflight_state()
                journal = {'schema': 1, 'module': MODULE, 'kind': KIND, 'transaction_id': self.core._id(),
                           'status': 'APACHE_FPM_PREPARED', 'before': plan['observations'],
                           'target': plan['target'], 'links': plan['links'], 'written': []}
                self.core._journal(journal)
                try:
                    self.core._checked([plan['target']['binary'], '-t'])
                    journal['status'] = 'APACHE_FPM_APPLYING'
                    self.core._journal(journal)
                    for entry in plan['links']:
                        if entry['before'] != entry['after']:
                            # 先保存意圖；replace 已完成但 fsync 失敗時仍能辨識並回復。
                            journal['written'].append(entry)
                            self.core._journal(journal)
                            self._replace_alias(entry['path'], entry['before'], entry['after'])
                    self.core._checked(['apache2ctl', 'configtest'])
                    self._service_state(plan['observations']['services']['fpm'], True, True)
                    from .root_fpm import probe_values
                    if probe_values(self.host, plan['target']['socket']).get('sapi') != 'fpm-fcgi':
                        raise ValueError('FPM did not answer before the Apache restart.')
                    self._service_state(plan['observations']['services']['apache'], True,
                                        plan['observations']['services']['apache']['enabled'], restart=True)
                    verification = self._verify(plan['target'])
                    journal['after'] = self._snapshot()
                    if self._unmanaged(journal['after'], journal['links']) != self._unmanaged(journal['before'], journal['links']):
                        raise RootActionError('Apache/PHP inputs changed while applying.', 'EXTERNAL_EDIT')
                    journal['status'] = 'APACHE_FPM_APPLIED'
                    self.core._journal(journal)
                    return {'status': journal['status'], 'module': MODULE, 'transaction_id': journal['transaction_id'],
                            'message': 'Native Apache event and same-version PHP-FPM activated.', 'verification': verification}
                except Exception:
                    try:
                        self._unwind(journal)
                        journal['status'] = 'APACHE_FPM_ROLLED_BACK'
                        self.core._journal(journal)
                    except Exception:
                        journal['status'] = 'APACHE_FPM_RECOVERY_REQUIRED'
                        self.core._journal(journal)
                        raise RootActionError('Apache handler rollback needs inspection of its private transaction journal.', 'RECOVERY_REQUIRED') from None
                    raise RootActionError('Apache handler change failed; aliases and service states were restored.', 'APACHE_FPM_ROLLED_BACK') from None
        except Exception as error:
            raise self._error(error) from None

    def _restore_candidate(self):
        self.core._check_pending()
        candidates = [entry for entry in self.core._journals()
                      if entry.get('kind') == KIND and entry.get('status') == 'APACHE_FPM_APPLIED']
        if not candidates:
            raise RootActionError('No applied Apache handler transaction is available.', 'NO_BACKUP')
        journal = candidates[-1]
        if self._snapshot() != journal['after']:
            raise RootActionError('Apache/PHP configuration or service state changed; handler restore is blocked.', 'EXTERNAL_EDIT')
        return journal

    def _restore_summary(self, journal):
        plan = {'schema': 1, 'module': MODULE, 'action': 'restore', 'transaction_id': journal['transaction_id'],
                'target': journal['target'],
                'links': [{'path': entry['path'], 'before': entry['after'], 'after': entry['before']}
                          for entry in journal['links']],
                'services': journal['before']['services'], 'after': journal['after'],
                'notes': ['Restore every changed native alias and both previous service states.',
                          'Apache restarts only if it was originally active; an originally inactive Apache remains inactive.',
                          'Any external Apache/PHP input, alias or service-state change blocks this restore. HTTP application health remains unverified.']}
        plan['fingerprint'] = _fingerprint(plan)
        return plan

    def restore_plan(self):
        try:
            self.core._gate('apache')
            return self._restore_summary(self._restore_candidate())
        except Exception as error:
            raise self._error(error) from None

    def restore(self, preview):
        try:
            self.core._gate('apache')
            with self.core._lock():
                journal = self._restore_candidate()
                if (not isinstance(preview, dict) or _fingerprint(preview) != preview.get('fingerprint')
                        or self._restore_summary(journal)['fingerprint'] != preview['fingerprint']):
                    raise RootActionError('Apache restore plan changed; review the current transaction.', 'STALE_PLAN')
                journal['status'] = 'APACHE_FPM_RESTORING'
                self.core._journal(journal)
                try:
                    self._unwind(journal)
                except Exception:
                    journal['status'] = 'APACHE_FPM_RECOVERY_REQUIRED'
                    self.core._journal(journal)
                    raise RootActionError('Apache handler restore needs inspection of its private journal.', 'RECOVERY_REQUIRED') from None
                journal['status'] = 'APACHE_FPM_RESTORED'
                self.core._journal(journal)
                return {'status': journal['status'], 'module': MODULE, 'transaction_id': journal['transaction_id'],
                        'message': 'Original Apache handler aliases and Apache/FPM service states restored.',
                        'verification': {'http_application': False, 'detail': 'Configuration and service restoration checked; HTTP applications unverified.'}}
        except Exception as error:
            raise self._error(error) from None

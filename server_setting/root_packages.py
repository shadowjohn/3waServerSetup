"""Selected APT packages only; upgrades remain in System updates.

APT metadata is read from the configured indexes. No repository or source URL
is accepted from the caller, and no index refresh happens during installation.
"""
import hashlib
from pathlib import Path
import re
import stat
import subprocess

from .root_actions import Host, RootManager, RootActionError, _fingerprint
from . import root_apt


NAME = re.compile(r'[a-z0-9][a-z0-9+.-]{0,127}')
INSTALL = ['apt-get', '--no-upgrade', '--no-remove', '--yes',
           '-o', 'Dpkg::Options::=--force-confdef', '-o', 'Dpkg::Options::=--force-confold']
MAX_SEARCH = 50


def _names(values, maximum=32):
    if (not isinstance(values, (list, tuple)) or not 1 <= len(values) <= maximum
            or any(not isinstance(name, str) or not NAME.fullmatch(name) or name.endswith('.deb') for name in values)):
        raise RootActionError(f'Use 1 to {maximum} plain APT package names; URLs, files, versions and flags are not accepted.', 'PACKAGES_INVALID')
    return sorted(set(values))


def _records(text):
    records = []
    current = {}
    for line in [*text.splitlines(), '']:
        if not line:
            if current:
                records.append(current)
                current = {}
        elif not line[0].isspace():
            key, separator, value = line.partition(': ')
            if separator:
                if key in current:
                    raise RootActionError('APT package metadata has duplicate fields.', 'PACKAGES_METADATA')
                current[key] = value
    return records


class PackageHost(Host):
    def run(self, argv, timeout=30):
        if argv and argv[0] == 'apt-cache':
            if (not isinstance(argv, (list, tuple)) or any(not isinstance(arg, str) or '\0' in arg for arg in argv)
                    or type(timeout) not in {int, float} or not 0 < timeout <= 900):
                raise RootActionError('Invalid bounded package metadata request.', 'UNSAFE_COMMAND')
            if self.root != Path('/').absolute():
                raise RootActionError('Prefixed package hosts require an explicit command test boundary.')
            from .trusted_commands import resolve
            executable = resolve('apt-cache')
            if executable is None:
                raise RootActionError('A trusted apt-cache command is unavailable.', 'MISSING_COMMAND')
            try:
                return subprocess.run([executable, *argv[1:]], stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, errors='replace', timeout=timeout, shell=False, check=False,
                    env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root', 'LANG': 'C', 'LC_ALL': 'C'})
            except subprocess.TimeoutExpired:
                raise RootActionError('Package metadata request timed out.', 'TIMEOUT') from None
            except OSError:
                raise RootActionError('Package metadata request failed.', 'PACKAGES_METADATA') from None
        return super().run(argv, timeout)


class PackageManager:
    def __init__(self, host=None):
        self.host = host or PackageHost()
        self.core = RootManager(self.host)

    def _gate(self):
        self.core._gate('time')

    def _checked(self, argv, timeout=30, missing_ok=False):
        result = self.host.run(argv, timeout=timeout)
        if len(result.stdout) > 8 * 1024 * 1024 or (result.returncode and not missing_ok):
            raise RootActionError('APT package metadata or simulation failed; native diagnostic output was discarded.', 'PACKAGES_NOT_READY')
        return result.stdout

    def _installed(self, names):
        output = self._checked(['dpkg-query', '-W', '-f=${binary:Package}\t${db:Status-Abbrev}\t${Version}\t${Installed-Size}\n', '--', *names], missing_ok=True)
        result = {}
        for line in output.splitlines():
            fields = line.split('\t')
            if len(fields) != 4 or not re.fullmatch(root_apt.PACKAGE, fields[0]):
                raise RootActionError('Unknown dpkg package status format.', 'PACKAGES_METADATA')
            if fields[1] != 'ii ':
                continue
            if not re.fullmatch(root_apt.VERSION, fields[2]) or not fields[3].isdigit():
                raise RootActionError('Unknown installed package metadata.', 'PACKAGES_METADATA')
            name = fields[0]
            if name in result:
                raise RootActionError('Ambiguous installed package architecture.', 'PACKAGES_METADATA')
            result[name] = {'version': fields[2], 'size_bytes': int(fields[3]) * 1024}
            plain = name.split(':', 1)[0]
            if plain != name and plain not in result:
                result[plain] = result[name]
        return result

    def _status(self, names):
        output = self._checked(['apt-cache', 'policy', '--', *names])
        candidates = {}
        current = None
        for line in output.splitlines():
            if line and not line[0].isspace() and line.endswith(':'):
                current = line[:-1]
            elif current in names and line.strip().startswith('Candidate:'):
                value = line.strip().partition(':')[2].strip()
                if value != '(none)' and not re.fullmatch(root_apt.VERSION, value):
                    raise RootActionError('Unknown APT candidate version.', 'PACKAGES_METADATA')
                candidates[current] = None if value == '(none)' else value
        installed = self._installed(names)
        return [{'name': name, 'installed': name in installed,
                 'current': installed.get(name, {}).get('version'), 'candidate': candidates.get(name)} for name in names]

    def status(self, package_names=None):
        self._gate()
        if package_names is None:
            package_names = sorted({name for group in self._groups() for item in group['items'] for name in item['packages']})
        return {'supported': True, 'items': self._status(_names(package_names, MAX_SEARCH)),
                'detail': 'Installed package versions and configured APT candidates; runtime component loading is not inferred.'}

    def search(self, query):
        self._gate()
        if (not isinstance(query, str) or not 1 <= len(query.strip()) <= 80
                or not re.fullmatch(r'[A-Za-z0-9+. -]+', query) or len(query.split()) > 4):
            raise RootActionError('Use up to four plain package search words, at most 80 characters.', 'PACKAGES_INVALID')
        output = self._checked(['apt-cache', 'search', '--', *[re.escape(word) for word in query.split()]], timeout=20)
        result = {}
        for line in output.splitlines():
            name, separator, description = line.partition(' - ')
            if separator and NAME.fullmatch(name) and not name.endswith('.deb'):
                result[name] = {'name': name, 'description': ''.join(c for c in description[:300] if c.isprintable())}
        return [result[name] for name in sorted(result)[:MAX_SEARCH]]

    def _metadata(self, specs):
        return _records(self._checked(['apt-cache', 'show', '--no-all-versions', '--', *specs]))

    def _groups(self):
        def item(name, label=None, detail=''):
            return {'id': name, 'label': label or name, 'packages': [name], 'detail': detail}
        def group(identifier, label, names, detail):
            return {'id': identifier, 'label': label, 'items': [item(name) for name in names], 'detail': detail}
        groups = [group('gis_cli', 'GIS command line tools', ['proj-bin', 'gdal-bin', 'geos-bin', 'spatialite-bin'], 'PROJ, GDAL, GEOS and SpatiaLite command line programs.'),
                  group('mapserver', 'MapServer', ['cgi-mapserver', 'mapserver-bin'], 'Package installation only; no vhost, CGI exposure or mapfile is configured by this tool.'),
                  group('python_gis', 'Python GIS', ['python3-gdal', 'python3-pyproj', 'python3-shapely', 'python3-geopandas'], 'Ubuntu system Python bindings; not installed into this tool virtualenv.'),
                  group('gis_headers', 'GIS development headers', ['libproj-dev', 'libgdal-dev', 'libgeos-dev', 'libspatialite-dev'], 'Headers and development libraries; these do not compile custom PHP modules.'),
                  group('base', 'Base tools', ['ntpsec-ntpdate', 'vim', 'make', 'screen', 'wget', 'ipset', 'iptables', 'curl', 'net-tools'], 'ntpsec-ntpdate is the Ubuntu 26.04 replacement package; installation does not run a time correction.')]
        groups[-1]['items'][0].update(id='ntpdate', label='ntpdate', provided_by='ntpsec-ntpdate', component_status='provided')
        meta = self._metadata(['php-fpm'])
        targets = {match for row in meta for match in re.findall(r'\b(php[0-9]+\.[0-9]+)-fpm\b', row.get('Depends', ''))}
        php = next(iter(targets)) if len(targets) == 1 else None
        common = php + '-common' if php else None
        php_items = [item('php-gd'), item('php-mbstring')]
        for name in ('php-exif', 'php-pdo'):
            php_items.append({'id': name, 'label': name, 'packages': [common] if common else [],
                              'provided_by': common or 'unknown PHP common provider', 'component_status': 'provided',
                              'detail': 'Provided by the version-specific PHP common package; installed package does not prove runtime extension loading.'})
        php_items += [{'id': 'php-json', 'label': 'php-json', 'packages': [], 'component_status': 'builtin',
                       'provided_by': (php + '-cli / ' + php + '-fpm') if php else 'PHP runtime',
                       'runtime_providers': [php + '-cli', php + '-fpm'] if php else [],
                       'detail': 'JSON is built into PHP 8 CLI/FPM. This is a runtime component, not a missing extension package.'},
                      item('php-mysql'), item('php-sqlite3', 'php-pdo-sqlite (official SQLite foundation)', 'Official distro SQLite/PDO package. FeatherMountain custom compiler is a separate operation.'),
                      item('php-zip'), item('php-xml'), item('libapache2-mod-xsendfile'),
                      {'id': 'php-pdo-sqlite-custom', 'label': 'FeatherMountain custom PDO SQLite', 'packages': [],
                       'component_status': 'external build', 'detail': 'Separate custom compilation workflow; selecting official php-sqlite3 does not build or verify this module.'}]
        groups.append({'id': 'php', 'label': 'PHP components and Apache XSendfile', 'items': php_items,
                       'detail': 'Real APT packages and provided/built-in components are distinguished. No automatic service or vhost configuration is performed by this tool.'})
        return groups

    def catalog(self):
        self._gate()
        groups = self._groups()
        names = sorted({name for group in groups for item in group['items'] for name in item['packages'] + item.get('runtime_providers', [])})
        rows = {row['name']: row for row in self._status(names)}
        for group in groups:
            group['packages'] = sorted({name for item in group['items'] for name in item['packages']})
            for item in group['items']:
                providers = item.pop('runtime_providers', [])
                if providers:
                    found = next((rows[name] for name in providers if rows[name]['installed']), None)
                    item.update(installed=bool(found), current=found['current'] if found else None,
                                candidate=found['candidate'] if found else None)
                elif item['packages']:
                    value = rows[item['packages'][0]]
                    item.update({key: value[key] for key in ('installed', 'current', 'candidate')})
                else:
                    item.update(installed=None, current=None, candidate=None)
        return groups

    def _state(self):
        digest = hashlib.sha256(root_apt._state(self.host).encode())
        paths = ['/etc/apt/sources.list', '/etc/apt/preferences', '/etc/apt/apt.conf']
        for directory in ('/etc/apt/sources.list.d', '/etc/apt/preferences.d', '/etc/apt/apt.conf.d'):
            path = self.host.path(directory)
            if path.exists():
                paths.extend('/' + child.relative_to(self.host.root).as_posix() for child in sorted(path.iterdir()) if child.is_file() or child.is_symlink())
        for name in sorted(paths):
            path = self.host.path(name)
            digest.update(name.encode() + b'\0')
            if path.exists():
                if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > 4 * 1024 * 1024:
                    raise RootActionError('Unsupported APT configuration input.', 'UNSAFE_STATE')
                digest.update(path.read_bytes())
            digest.update(b'\0')
        return digest.hexdigest()

    def plan(self, package_names):
        names = _names(package_names)
        self._gate()
        state = self._state()
        observed = self._status(names)
        selected = [row['name'] for row in observed if not row['installed']]
        if any(not row['candidate'] for row in observed if not row['installed']):
            raise RootActionError('A selected package has no real configured APT candidate; choose a catalog provider or refresh indexes separately.', 'PACKAGES_NOT_AVAILABLE')
        command = [*INSTALL, 'install', '--', *selected]
        packages = []
        if selected:
            simulation = self._checked([*INSTALL, '--simulate', 'install', '--', *selected], timeout=900)
            packages, _ = root_apt._simulation(simulation)
            if len(packages) > 1024 or len({row['name'] for row in packages}) != len(packages):
                raise RootActionError('APT dependency plan is too large or ambiguous.', 'PACKAGES_UNSAFE_PLAN')
            if any(row['installed'] is not None for row in packages):
                raise RootActionError('Installing this selection requires upgrading existing packages; use System updates separately first.', 'PACKAGES_UPGRADE_REQUIRED')
            if not set(selected) <= {row['name'].split(':')[0] for row in packages}:
                raise RootActionError('APT did not plan every selected real package.', 'PACKAGES_UNSAFE_PLAN')
        metadata = self._metadata([row['name'] + '=' + row['candidate'] for row in packages]) if packages else []
        changes = []
        for row in packages:
            matches = [record for record in metadata if record.get('Package') == row['name'].split(':')[0]
                       and record.get('Version') == row['candidate']]
            if len(matches) != 1 or any(not matches[0].get(field, '').isdigit() for field in ('Size', 'Installed-Size')):
                raise RootActionError('Candidate archive/disk size could not be verified.', 'PACKAGES_METADATA')
            changes.append({**row, 'action': 'install', 'download_bytes': int(matches[0]['Size']),
                            'disk_delta_bytes': int(matches[0]['Installed-Size']) * 1024})
        if self._state() != state:
            raise RootActionError('APT/dpkg state changed during preview.', 'STALE_PLAN')
        changes.sort(key=lambda row: row['name'])
        result = {'schema': 1, 'module': 'packages', 'names': names, 'install_names': selected,
                  'observed': observed, 'changes': changes, 'command': command, 'apt_state_sha256': state,
                  'download_bytes': sum(row['download_bytes'] for row in changes),
                  'disk_bytes': sum(row['disk_delta_bytes'] for row in changes),
                  'notes': ['Install only: existing packages are retained; dependency upgrades must be handled in System updates first.',
                            'Sizes describe complete candidate archives and installed package data; an existing archive cache can reduce network transfer.',
                            'Configured APT indexes are used without apt update. No removals, autoremove, full upgrade or reboot is performed.',
                            'Modified conffiles are preserved. Distro package scripts may start/restart services or enable their default integration.',
                            'This tool does not configure vhosts, expose CGI or compile custom PHP extensions.',
                            'Package operations cannot be undone by configuration Restore last; failures may leave partial package changes.']}
        result['fingerprint'] = _fingerprint(result)
        return result

    def install(self, preview):
        self._gate()
        if not isinstance(preview, dict) or preview.get('fingerprint') != _fingerprint(preview):
            raise RootActionError('Package preview was modified.', 'TAMPERED_PLAN')
        with self.core._lock():
            self.core._check_pending()
            trusted = self.plan(preview.get('names'))
            if trusted['fingerprint'] != preview['fingerprint']:
                raise RootActionError('APT/dpkg state changed after package preview.', 'STALE_PLAN')
            if not trusted['changes']:
                return {'status': 'NO_CHANGE', 'module': 'packages', 'names': trusted['names'],
                        'versions': trusted['observed'], 'message': 'All selected packages are installed; their existing versions were retained.'}
            self.core._preflight_state()
            identifier = self.core._id()
            journal = {'schema': 1, 'module': 'packages', 'kind': 'packages', 'transaction_id': identifier,
                       'status': 'PACKAGES_RUNNING', 'names': trusted['names'], 'changes': trusted['changes'],
                       'fingerprint': trusted['fingerprint']}
            self.core._journal(journal)
            try:
                result = self.host.run(trusted['command'], timeout=900)
                if result.returncode:
                    raise RootActionError('APT did not complete.', 'PACKAGES_FAILED')
                names = [row['name'] for row in trusted['changes']]
                installed = self._installed(names)
                if any(installed.get(row['name'], {}).get('version') != row['candidate'] for row in trusted['changes']):
                    raise RootActionError('dpkg did not verify every previewed package version.', 'PACKAGES_FAILED')
            except Exception:
                journal['status'] = 'PACKAGES_FAILED'
                self.core._journal(journal)
                return self.core._log_result({'status': 'PACKAGES_FAILED', 'module': 'packages', 'transaction_id': identifier,
                    'names': trusted['names'], 'package_state_may_have_changed': True,
                    'message': 'Package installation failed or timed out; inspect native APT/dpkg state. No automatic rollback was attempted.'})
            journal['status'] = 'PACKAGES_INSTALLED'
            self.core._journal(journal)
            return self.core._log_result({'status': 'PACKAGES_INSTALLED', 'module': 'packages', 'transaction_id': identifier,
                'names': trusted['names'], 'changes': trusted['changes'],
                'versions': [{'name': row['name'], 'current': row['candidate']} for row in trusted['changes']],
                'reboot_required': self.host.path('/run/reboot-required').exists(),
                'message': 'Previewed package versions verified with dpkg. Service/application health is not inferred.'})

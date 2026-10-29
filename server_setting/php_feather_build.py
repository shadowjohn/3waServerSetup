"""Authenticated Ubuntu PDO SQLite extension build; never activates an ini.

Only fixed source/dependency operations run as root. Source extraction,
configure, compilation and all native-code probes run in a bounded systemd
unit using the dedicated unprivileged builder account.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

WORKER_FILE = Path(__file__).resolve()
_WORKER_SOURCE_SHA256 = hashlib.sha256(WORKER_FILE.read_bytes()).hexdigest()

if not __package__:
    sys.path.insert(0, str(WORKER_FILE.parents[1]))
    __package__ = 'server_setting'
from .root_actions import Host, RootManager, RootActionError, _fingerprint, _json
from .root_packages import PackageManager, PackageHost
from .trusted_commands import resolve
from .operation_progress import phase_context, emit_progress, run_captured


STATE = '/var/lib/3wa-server-setup/feather-build'
MODULES = '/opt/3waServerSetting/modules/feather-pdo-sqlite'
BUILDER = 'threewa-php-build'
KEYRING = '/usr/share/keyrings/ubuntu-archive-keyring.gpg'
GATE = 'pdo_sqlite.feather_sql_load_extension'
PATCH_PROFILE = {'id': 'feather-sql-load-system-v1', 'gate': GATE, 'default': 'Off',
                 'scope': 'PHP_INI_SYSTEM', 'constructor': 'sqlite3_enable_load_extension(H->db, 1)',
                 'pdo_driver_option': False}
_INI_DEFINITION = (b'PHP_INI_BEGIN()\n'
                   b'\tPHP_INI_ENTRY("pdo_sqlite.feather_sql_load_extension", "0", PHP_INI_SYSTEM, NULL)\n'
                   b'PHP_INI_END()\n\n')
_CONSTRUCTOR_GATE = (b'#ifndef PDO_SQLITE_OMIT_LOAD_EXTENSION\n'
                     b'\tif (INI_BOOL("pdo_sqlite.feather_sql_load_extension")) {\n'
                     b'\t\ti = sqlite3_enable_load_extension(H->db, 1);\n'
                     b'\t\tif (i != SQLITE_OK) {\n\t\t\tpdo_sqlite_error(dbh);\n\t\t\tgoto cleanup;\n\t\t}\n\t}\n'
                     b'#endif\n\n')
PATCH_SHA256 = hashlib.sha256(_INI_DEFINITION + _CONSTRUCTOR_GATE + b'REGISTER_INI_ENTRIES();\nUNREGISTER_INI_ENTRIES();\n').hexdigest()
_HASH = re.compile(r'[0-9a-f]{64}')
_VERSION = re.compile(r'[0-9A-Za-z.+:~_-]+')


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _file(path, maximum=64 * 1024 * 1024, root_owned=True):
    path = Path(path)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum
            or (os.name == 'posix' and root_owned and (info.st_uid != 0 or info.st_mode & 0o022))):
        raise RootActionError('An untrusted source or artifact file was rejected.', 'FEATHER_UNSAFE_FILE')
    return path.read_bytes()


def patch_sources(files):
    if set(files) != {'pdo_sqlite.c', 'sqlite_driver.c'}:
        raise RootActionError('The expected PDO SQLite source files are required.', 'FEATHER_SOURCE_LAYOUT')
    if any(GATE.encode() in data for data in files.values()):
        raise RootActionError('An already modified PDO SQLite source was rejected.', 'FEATHER_SOURCE_LAYOUT')
    module = files['pdo_sqlite.c']
    driver = files['sqlite_driver.c']
    definitions = _INI_DEFINITION
    replacements = [(b'static zend_class_entry *pdosqlite_ce;', definitions + b'static zend_class_entry *pdosqlite_ce;'),
                    (b'PHP_MINIT_FUNCTION(pdo_sqlite)\n{', b'PHP_MINIT_FUNCTION(pdo_sqlite)\n{\n\tREGISTER_INI_ENTRIES();'),
                    (b'PHP_MSHUTDOWN_FUNCTION(pdo_sqlite)\n{', b'PHP_MSHUTDOWN_FUNCTION(pdo_sqlite)\n{\n\tUNREGISTER_INI_ENTRIES();')]
    for before, after in replacements:
        if module.count(before) != 1:
            raise RootActionError('Unknown or ambiguous PDO SQLite module source.', 'FEATHER_SOURCE_LAYOUT')
        module = module.replace(before, after, 1)
    anchor = b'\tsqlite3_set_authorizer(H->db, authorizer, H);'
    if driver.count(anchor) != 1 or driver.count(b'static int pdo_sqlite_handle_factory(') != 1:
        raise RootActionError('Unknown PDO SQLite constructor layout.', 'FEATHER_SOURCE_LAYOUT')
    conditional = _CONSTRUCTOR_GATE
    driver = driver.replace(anchor, conditional + anchor, 1)
    return {'pdo_sqlite.c': module, 'sqlite_driver.c': driver}


def _deb822(text):
    records, current, previous = [], {}, None
    for line in [*text.splitlines(), '']:
        if not line.strip():
            if current:
                records.append(current)
            current, previous = {}, None
        elif line.startswith('#'):
            continue
        elif line[0].isspace():
            if previous is None:
                raise RootActionError('Invalid deb822 input.', 'FEATHER_SOURCE_LAYOUT')
            current[previous] += '\n' + line[1:]
        else:
            key, separator, value = line.partition(':')
            if not separator or key in current:
                raise RootActionError('Invalid or duplicate deb822 field.', 'FEATHER_SOURCE_LAYOUT')
            current[key] = value.strip()
            previous = key
    return records


def source_profile(host):
    core = RootManager(host)
    core._trusted_target(host.path(KEYRING))
    keyring = _file(host.path(KEYRING), 4 * 1024 * 1024)
    directory = host.path('/etc/apt/sources.list.d')
    candidates = sorted(directory.glob('*.sources')) if directory.is_dir() else []
    records, inputs = [], []
    for candidate in candidates:
        absolute = '/' + candidate.relative_to(host.root).as_posix()
        core._trusted_target(host.path(absolute))
        data = _file(host.path(absolute), 1024 * 1024)
        accepted = []
        for row in _deb822(data.decode('utf-8')):
            if row.get('Enabled', 'yes') == 'no':
                continue
            if row.get('Signed-By') != KEYRING:
                continue
            allowed = {'Types', 'URIs', 'Suites', 'Components', 'Signed-By', 'Architectures', 'Enabled'}
            if set(row) - allowed or not {'Types', 'URIs', 'Suites', 'Components', 'Signed-By'} <= set(row):
                raise RootActionError('Only ordinary signed Ubuntu source entries are supported.', 'FEATHER_SOURCE_LAYOUT')
            if not set(row['Types'].split()) <= {'deb', 'deb-src'}:
                raise RootActionError('Unknown Ubuntu APT source type.', 'FEATHER_SOURCE_LAYOUT')
            suites = row['Suites'].split()
            if not suites or not set(suites) <= {'resolute', 'resolute-updates', 'resolute-security', 'resolute-backports'}:
                raise RootActionError('The source suite does not match Ubuntu 26.04.', 'FEATHER_SOURCE_LAYOUT')
            if not row['Components'].split() or not set(row['Components'].split()) <= {'main', 'restricted', 'universe', 'multiverse'}:
                raise RootActionError('Unknown Ubuntu source component.', 'FEATHER_SOURCE_LAYOUT')
            for uri in row['URIs'].split():
                parsed = urlsplit(uri)
                if (parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password or parsed.query or parsed.fragment
                        or not parsed.hostname or not (parsed.hostname == 'security.ubuntu.com' or parsed.hostname == 'archive.ubuntu.com'
                                                       or parsed.hostname.endswith('.archive.ubuntu.com'))
                        or parsed.path.rstrip('/') != '/ubuntu'):
                    raise RootActionError('A native Ubuntu archive URI is required.', 'FEATHER_SOURCE_LAYOUT')
            generated = {key: value for key, value in row.items() if key != 'Enabled'}
            generated['Types'] = 'deb-src'
            accepted.append(generated)
        if accepted:
            inputs.append({'path': absolute, 'sha256': _sha(data)})
            records.extend(accepted)
    if not records:
        raise RootActionError('No authenticated native Ubuntu deb822 archive source is available.', 'FEATHER_SOURCE_LAYOUT')
    text = '\n\n'.join('\n'.join(key + ': ' + row[key] for key in ('Types', 'URIs', 'Suites', 'Components', 'Signed-By', 'Architectures') if key in row) for row in records) + '\n'
    return {'deb_src': text, 'inputs': inputs, 'keyring': KEYRING, 'keyring_sha256': _sha(keyring),
            'authentication': 'APT signed Sources index and SHA256, using the configured Ubuntu archive keyring',
            'system_sources_changed': False}


def verify_source_downloads(directory, version):
    directory = Path(directory)
    dscs = list(directory.glob('*.dsc'))
    if len(dscs) != 1:
        raise RootActionError('Exactly one authenticated source descriptor is required.', 'FEATHER_SOURCE_LAYOUT')
    data = _file(dscs[0], 1024 * 1024)
    text = data.decode('utf-8').replace('\r\n', '\n')
    if text.startswith('-----BEGIN PGP SIGNED MESSAGE-----'):
        text = text.split('\n\n', 1)[1].split('-----BEGIN PGP SIGNATURE-----', 1)[0]
        text = '\n'.join(line[2:] if line.startswith('- ') else line for line in text.splitlines())
    records = _deb822(text)
    if (len(records) != 1 or records[0].get('Source') != 'php8.5' or records[0].get('Version') != version
            or records[0].get('Format') != '3.0 (quilt)'):
        raise RootActionError('Ubuntu PHP source version does not match the installed binary package.', 'FEATHER_SOURCE_VERSION')
    fields = records[0]
    archives = []
    for line in fields.get('Checksums-Sha256', '').splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if (len(parts) != 3 or not _HASH.fullmatch(parts[0]) or not parts[1].isdigit()
                or not re.fullmatch(r'php8\.5_[A-Za-z0-9.+~_-]+\.(?:orig\.tar\.(?:xz|gz|bz2)(?:\.asc)?|debian\.tar\.(?:xz|gz))', parts[2])
                or not 0 < int(parts[1]) <= 512 * 1024 * 1024):
            raise RootActionError('Unsupported PHP source archive descriptor.', 'FEATHER_SOURCE_LAYOUT')
        archive = directory / parts[2]
        content = _file(archive, 512 * 1024 * 1024)
        if len(content) != int(parts[1]) or _sha(content) != parts[0]:
            raise RootActionError('PHP source archive integrity failed.', 'FEATHER_SOURCE_INTEGRITY')
        archives.append({'name': parts[2], 'size': len(content), 'sha256': parts[0]})
    if (not 2 <= len(archives) <= 8 or len({item['name'] for item in archives}) != len(archives)
            or not any('.orig.tar.' in item['name'] and not item['name'].endswith('.asc') for item in archives)
            or not any('.debian.tar.' in item['name'] for item in archives)):
        raise RootActionError('Unknown PHP source archive set.', 'FEATHER_SOURCE_LAYOUT')
    return {'source_package': 'php8.5', 'package_version': version, 'source_sha256': _sha(data),
            'dsc': dscs[0].name, 'dsc_size': len(data), 'files': archives}


def unit_command(workspace, job, identifier):
    if (not re.fullmatch(r'/var/tmp/threewa-feather-[A-Za-z0-9_-]+', workspace)
            or job != workspace + '/job.json' or not re.fullmatch(r'[a-f0-9]{12}', identifier)):
        raise RootActionError('Unsafe build unit path.', 'FEATHER_UNSAFE_FILE')
    properties = ['User=' + BUILDER, 'Group=' + BUILDER, 'KillMode=control-group', 'NoNewPrivileges=yes',
                  'ProtectSystem=strict', 'ProtectHome=yes', 'PrivateTmp=yes', 'PrivateNetwork=yes',
                  'RestrictAddressFamilies=AF_UNIX', 'BindPaths=' + workspace, 'ReadWritePaths=' + workspace, 'RuntimeMaxSec=900',
                  'TimeoutStartSec=900', 'TimeoutStopSec=5', 'UMask=0077', 'TasksMax=128',
                  'Environment=PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=' + workspace + ' LANG=C LC_ALL=C',
                  'UnsetEnvironment=LD_PRELOAD LD_LIBRARY_PATH PYTHONPATH PYTHONHOME BASH_ENV ENV PHPRC PHP_INI_SCAN_DIR APT_CONFIG']
    return ['systemd-run', '--quiet', '--wait', '--pipe', '--collect', '--service-type=exec',
            '--expand-environment=no',
            '--unit=threewa-feather-build-' + identifier, *['--property=' + value for value in properties],
            '/usr/bin/python3', '-I', str(WORKER_FILE), '--worker', job]


def _elf(data):
    return (len(data) >= 64 and data[:6] == b'\x7fELF\x02\x01'
            and struct.unpack_from('<HH', data, 16) == (3, 62))


def validate_artifact(artifact, host=None):
    host = host or FeatherHost()
    required = {'module_path', 'sha256', 'abi', 'package_version', 'php_version', 'arch', 'nts',
                'source_sha256', 'patch_sha256', 'probe_so_path', 'probe_sha256', 'probe_function', 'probe_value', 'buildmanifest'}
    if not isinstance(artifact, dict) or not required <= set(artifact):
        raise RootActionError('Incomplete Feather build artifact.', 'FEATHER_ARTIFACT_INVALID')
    directory = str(Path(artifact['module_path']).parent).replace('\\', '/')
    if (not re.fullmatch(re.escape(MODULES) + r'/[a-f0-9]{64}', directory)
            or artifact['module_path'] != directory + '/pdo_sqlite.so'
            or artifact['probe_so_path'] != directory + '/featherproof.so'
            or artifact['arch'] != 'amd64' or artifact['nts'] is not True or artifact['abi'] != '20250925'
            or not re.fullmatch(r'threewa_[a-f0-9]{12}', artifact['probe_function'])
            or not re.fullmatch(r'feather-[a-f0-9]{12}', artifact['probe_value'])
            or not re.fullmatch(re.escape(STATE) + r'/manifests/[a-f0-9]{64}\.json', artifact['buildmanifest'])
            or any(not isinstance(artifact[key], str) or not _HASH.fullmatch(artifact[key]) for key in ('sha256', 'probe_sha256', 'source_sha256', 'patch_sha256'))):
        raise RootActionError('Unsupported Feather artifact identity.', 'FEATHER_ARTIFACT_INVALID')
    core = RootManager(host)
    native = host.inspect()
    if any(artifact[key] != native[key] for key in ('php_version', 'package_version', 'abi', 'arch', 'nts')):
        raise RootActionError('Feather artifact does not match the current PHP binary package.', 'FEATHER_ARTIFACT_INVALID')
    for path_key, digest_key in (('module_path', 'sha256'), ('probe_so_path', 'probe_sha256')):
        target = host.path(artifact[path_key])
        core._trusted_target(target)
        data = _file(target)
        if not _elf(data) or _sha(data) != artifact[digest_key]:
            raise RootActionError('Feather artifact bytes changed.', 'FEATHER_ARTIFACT_INVALID')
        if os.name == 'posix' and stat.S_IMODE(target.stat().st_mode) != 0o644:
            raise RootActionError('Feather artifact permissions must be 0644.', 'FEATHER_ARTIFACT_INVALID')
    manifest = host.path(artifact['buildmanifest'])
    core._trusted_file(manifest)
    value = json.loads(_file(manifest, 4 * 1024 * 1024).decode('utf-8'))
    if value.get('artifact') != artifact or value.get('verified') is not True:
        raise RootActionError('Feather private manifest disagrees with the artifact.', 'FEATHER_ARTIFACT_INVALID')
    return dict(artifact)


class FeatherHost(PackageHost):
    def run(self, argv, timeout=30, cwd=None, apt_config=None):
        names = {'apt-cache', 'apt-get', 'dpkg-query', 'dpkg', 'php8.5', 'php-fpm8.5', 'systemctl', 'systemd-run', 'useradd', 'php-config8.5', 'phpize8.5'}
        if (not isinstance(argv, (list, tuple)) or not argv or argv[0] not in names
                or any(not isinstance(arg, str) or '\0' in arg for arg in argv) or not 0 < timeout <= 900):
            raise RootActionError('Invalid bounded Feather build command.', 'UNSAFE_COMMAND')
        if self.root != Path('/').absolute():
            raise RootActionError('Prefixed build hosts require an explicit native command boundary.')
        binary = resolve(argv[0])
        if not binary:
            raise RootActionError('A required trusted Feather build executable is unavailable.', 'MISSING_COMMAND')
        env = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root', 'LANG': 'C', 'LC_ALL': 'C',
               'DEBIAN_FRONTEND': 'noninteractive', 'NEEDRESTART_MODE': 'l'}
        if apt_config is not None:
            env['APT_CONFIG'] = str(apt_config)
        try:
            result = run_captured([binary, *argv[1:]], timeout=timeout, cwd=cwd, env=env, command=argv[0])
            if result.output_truncated and argv[0] not in {'apt-get', 'systemd-run'}:
                raise RootActionError('Build command output exceeded its bound.', 'FEATHER_BUILD_FAILED')
            return result
        except subprocess.TimeoutExpired:
            raise RootActionError('Feather command timed out.', 'TIMEOUT') from None
        except OSError:
            raise RootActionError('Feather command could not execute.', 'FEATHER_BUILD_FAILED') from None

    def checked(self, argv, **kwargs):
        value = self.run(argv, **kwargs)
        if value.returncode:
            raise RootActionError('Feather source/build command failed; raw diagnostics were discarded.', 'FEATHER_BUILD_FAILED')
        return value.stdout

    def inspect(self):
        names = ['php8.5-cli', 'php8.5-common', 'php8.5-fpm', 'php8.5-sqlite3', 'libsqlite3-0']
        text = self.checked(['dpkg-query', '-W', '-f=${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\t${Architecture}\t${db:Status-Abbrev}\n', '--', *names])
        rows = {}
        for line in text.splitlines():
            fields = line.split('\t')
            if len(fields) != 6 or fields[5] != 'ii ' or fields[4] != 'amd64':
                raise RootActionError('A complete native amd64 PHP/SQLite installation is required.', 'FEATHER_NOT_READY')
            rows[fields[0].split(':')[0]] = fields
        if set(rows) != set(names):
            raise RootActionError('Native PHP 8.5 CLI/FPM/SQLite packages are required.', 'FEATHER_NOT_READY')
        version = rows['php8.5-cli'][1]
        if (not _VERSION.fullmatch(version) or 'ubuntu' not in version
                or any(row[1] != version or row[2] != 'php8.5' or row[3] != version for name, row in rows.items() if name.startswith('php'))):
            raise RootActionError('PHP packages do not share one exact Ubuntu source version.', 'FEATHER_NOT_READY')
        program = 'echo json_encode(["php_version"=>PHP_VERSION,"zts"=>(bool)PHP_ZTS,"debug"=>(bool)PHP_DEBUG,"extension_dir"=>ini_get("extension_dir")]);'
        info = json.loads(self.checked(['php8.5', '-n', '-r', program]))
        phpinfo = self.checked(['php8.5', '-n', '-i'])
        api = re.findall(r'^PHP API => ([0-9]+)$', phpinfo, re.M)
        if (info.get('php_version') != version.split('-', 1)[0] or info.get('zts') or info.get('debug')
                or api != ['20250925'] or info.get('extension_dir') != '/usr/lib/php/20250925'):
            raise RootActionError('Only native PHP 8.5 amd64 NTS API20250925 is supported.', 'FEATHER_NOT_READY')
        sqlite = rows['libsqlite3-0'][1]
        original = '/usr/lib/php/20250925/pdo_sqlite.so'
        return {'php_version': info['php_version'], 'package_version': version, 'abi': api[0], 'arch': 'amd64', 'nts': True,
                'debug': False, 'source_package': 'php8.5', 'sqlite_package_version': sqlite,
                'extension_dir': info['extension_dir'], 'original_module': original,
                'original_sha256': _sha(_file(self.path(original))), 'pdo_module': '/usr/lib/php/20250925/pdo.so'}


class _BuildDependencyHost:
    """Only build-dependency simulations suppress optional recommendations."""
    def __init__(self, host):
        self.host = host

    def __getattr__(self, key):
        return getattr(self.host, key)

    def run(self, argv, **kwargs):
        if argv and argv[0] == 'apt-get' and '--no-install-recommends' not in argv:
            argv = [argv[0], '--no-install-recommends', *argv[1:]]
        return self.host.run(argv, **kwargs)


class FeatherBuilder:
    def __init__(self, host=None):
        self.host = host or FeatherHost()
        self.core = RootManager(self.host)

    def _packages(self):
        return PackageManager(_BuildDependencyHost(self.host))

    def _worker_source(self):
        guidance = ('Run the Feather build from a root-owned, publicly readable and traversable '
                    'checkout under /opt; its worker must be visible to the systemd sandbox.')
        try:
            virtual = PurePosixPath('/') / WORKER_FILE.relative_to(self.host.root).as_posix()
        except ValueError:
            raise RootActionError(guidance, 'FEATHER_WORKER_PATH') from None
        if any(virtual.is_relative_to(prefix) for prefix in ('/root', '/home', '/run/user', '/tmp', '/var/tmp')):
            raise RootActionError(guidance, 'FEATHER_WORKER_PATH')
        try:
            self.core._trusted_target(WORKER_FILE)
            if not WORKER_FILE.lstat().st_mode & 0o004:
                raise RootActionError(guidance, 'FEATHER_WORKER_PATH')
            for parent in WORKER_FILE.parents:
                if parent.lstat().st_mode & 0o005 != 0o005:
                    raise RootActionError(guidance, 'FEATHER_WORKER_PATH')
                if parent == self.host.root:
                    break
            source = _file(WORKER_FILE, maximum=1024 * 1024)
        except (OSError, RootActionError):
            raise RootActionError(guidance, 'FEATHER_WORKER_PATH') from None
        if _sha(source) != _WORKER_SOURCE_SHA256:
            raise RootActionError('Loaded Feather worker source changed; restart the selected /opt checkout before building.', 'FEATHER_WORKER_CHANGED')

    def plan(self):
        self.core._gate('time')
        # Reuse the existing root path boundary; the frozen physical source also
        # prevents a later current-alias switch from choosing another worker.
        self._worker_source()
        native = self.host.inspect()
        if native.get('abi') != '20250925' or native.get('arch') != 'amd64' or native.get('nts') is not True or not native.get('php_version', '').startswith('8.5.'):
            raise RootActionError('Unsupported native PHP build profile.', 'FEATHER_NOT_READY')
        profile = source_profile(self.host)
        dependencies = self._packages().plan(['php8.5-dev', 'build-essential', 'make', 'pkg-config', 'libsqlite3-dev', 'dpkg-dev'])
        if '--no-install-recommends' not in dependencies['command']:
            dependencies['command'].insert(1, '--no-install-recommends')
        dependencies['fingerprint'] = _fingerprint(dependencies)
        observed = {item['name']: item for item in dependencies.get('observed', [])}
        for name, expected in (('php8.5-dev', native['package_version']), ('libsqlite3-dev', native['sqlite_package_version'])):
            row = observed.get(name)
            if row is None or (row['current'] if row['installed'] else row['candidate']) != expected:
                raise RootActionError('Exact matching PHP/SQLite development packages are unavailable; use System updates first.', 'FEATHER_DEPENDENCY_MISMATCH')
        result = {'schema': 1, 'module': 'php-feather-build', 'native': native, 'source_profile': profile,
                  'dependencies': dependencies, 'patch_profile': PATCH_PROFILE, 'patch_sha256': PATCH_SHA256,
                  'builder_account': {'name': BUILDER, 'system': True, 'shell': '/usr/sbin/nologin', 'home': '/nonexistent'},
                  'build': {'worker_file': str(WORKER_FILE), 'worker_sha256': _WORKER_SOURCE_SHA256,
                            'phpize': ['phpize8.5'], 'configure': ['sh', './configure', '--with-php-config=/usr/bin/php-config8.5', '--with-pdo-sqlite'],
                            'make': ['make', '-j2'], 'unit_limit_seconds': 900, 'make_install': False},
                  'notes': ['Fetch the exact Ubuntu source package through a private authenticated APT source cache; system sources are unchanged.',
                            'Only missing build dependencies are installed; existing dependency upgrades are refused.',
                            'A dedicated nologin system builder account is created if absent; an unknown existing same-name account is refused.',
                            'The builder account and build dependencies are not removed by ini restore.',
                            'Extract, phpize, configure, make and CLI probes run unprivileged without network in a bounded systemd cgroup.',
                            'Publish versioned root-owned artifacts only. No make install, packaged .so overwrite or CLI/FPM ini switch.',
                            'The SYSTEM gate defaults Off; explicit activation is a separate root opt-in. No new PDO driver option or application change.']}
        result['fingerprint'] = _fingerprint(result)
        return result

    def status(self):
        try:
            value = self.plan()
            return {'supported': True, 'native': value['native'], 'source_profile': value['source_profile'],
                    'dependencies': value['dependencies'], 'detail': 'Exact Ubuntu source extension build is available; no activation performed.'}
        except RootActionError as error:
            return {'supported': False, 'code': error.code, 'detail': str(error)}

    def _ensure_user(self):
        import pwd
        import grp
        marker = self.host.path(STATE + '/builder-user.json')
        try:
            account = pwd.getpwnam(BUILDER)
            if not marker.exists():
                raise RootActionError('An unknown same-name builder account exists.', 'FEATHER_BUILDER_ACCOUNT')
            self.core._trusted_file(marker)
            previous = json.loads(marker.read_text())
        except KeyError:
            self.host.checked(['useradd', '--system', '--user-group', '--no-create-home', '--home-dir', '/nonexistent', '--shell', '/usr/sbin/nologin', BUILDER])
            account = pwd.getpwnam(BUILDER)
            previous = None
        identity = {'name': account.pw_name, 'uid': account.pw_uid, 'gid': account.pw_gid,
                    'home': account.pw_dir, 'shell': account.pw_shell}
        if (not 0 < account.pw_uid < 1000 or account.pw_dir != '/nonexistent' or account.pw_shell != '/usr/sbin/nologin'
                or grp.getgrgid(account.pw_gid).gr_name != BUILDER
                or set(os.getgrouplist(BUILDER, account.pw_gid)) != {account.pw_gid}
                or previous is not None and previous != identity):
            raise RootActionError('The builder account is not the managed isolated system identity.', 'FEATHER_BUILDER_ACCOUNT')
        self.core._private_json(STATE + '/builder-user.json', identity)
        return identity

    def _download(self, profile, native, identifier):
        base = STATE + '/sources/' + identifier
        for relative in ('', '/empty', '/lists/partial', '/cache/archives/partial', '/logs', '/download'):
            self.core._secure_dir(base + relative)
        root = self.host.path(base)
        self.core._atomic(base + '/ubuntu.sources', profile['deb_src'].encode(), {'mode': 0o600, 'uid': 0, 'gid': 0, 'xattrs': {}})
        self.core._atomic(base + '/empty.conf', b'', {'mode': 0o600, 'uid': 0, 'gid': 0, 'xattrs': {}})
        config = {'Dir::Etc::parts': str(root / 'empty'), 'Dir::Etc::main': str(root / 'empty.conf'),
                  'Dir::Etc::sourcelist': str(root / 'ubuntu.sources'), 'Dir::Etc::sourceparts': str(root / 'empty'),
                  'Dir::Etc::netrc': str(root / 'empty.conf'), 'Dir::Etc::netrcparts': str(root / 'empty'),
                  'Dir::State::lists': str(root / 'lists'), 'Dir::State::status': '/var/lib/dpkg/status',
                  'Dir::Cache': str(root / 'cache'), 'Dir::Log': str(root / 'logs'),
                  'APT::Sandbox::User': 'root', 'APT::Get::AllowUnauthenticated': 'false',
                  'Acquire::AllowInsecureRepositories': 'false', 'Acquire::AllowDowngradeToInsecureRepositories': 'false',
                  'Acquire::Check-Valid-Until': 'true', 'Acquire::Check-Date': 'true',
                  'Acquire::Retries': '0', 'Acquire::http::Timeout': '30', 'Acquire::https::Timeout': '30'}
        text = ''.join(key + ' "' + value + '";\n' for key, value in config.items())
        self.core._atomic(base + '/apt.conf', text.encode(), {'mode': 0o600, 'uid': 0, 'gid': 0, 'xattrs': {}})
        options = {'apt_config': root / 'apt.conf', 'cwd': root / 'download', 'timeout': 900}
        with phase_context('source-download'):
            self.host.checked(['apt-get', '--error-on=any', 'update'], **options)
            self.host.checked(['apt-get', '--download-only', '--only-source', 'source', 'php8.5=' + native['package_version']], **options)
        return root / 'download', verify_source_downloads(root / 'download', native['package_version'])

    def _public_dir(self, absolute):
        path = self.host.path(absolute)
        missing, current = [], path
        while not current.exists():
            missing.append(current)
            current = current.parent
        for parent in [current, *current.parents]:
            if parent == self.host.root.parent and self.host.root != Path('/'):
                break
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or os.name == 'posix' and (info.st_uid != 0 or info.st_mode & 0o022):
                raise RootActionError('Artifact publication parent is not root controlled.', 'FEATHER_UNSAFE_FILE')
            if parent != self.host.root and os.name == 'posix' and info.st_mode & 0o005 != 0o005:
                raise RootActionError('Existing artifact publication parents must be publicly traversable/readable.', 'FEATHER_UNSAFE_FILE')
            if parent == self.host.root:
                break
        for directory in reversed(missing):
            directory.mkdir(mode=0o755)
            directory.chmod(0o755)
        return path

    def build(self, preview, transaction_id=None):
        if not isinstance(preview, dict) or preview.get('fingerprint') != _fingerprint(preview):
            raise RootActionError('Feather build preview was modified.', 'TAMPERED_PLAN')
        with self.core._lock():
            self.core._check_pending(ignore_id=transaction_id)
            trusted = self.plan()
            if trusted['fingerprint'] != preview['fingerprint']:
                raise RootActionError('PHP/APT/source state changed after preview.', 'STALE_PLAN')
            native = trusted['native']
            dependencies = trusted['dependencies']
            self.core._secure_dir(STATE)
            identifier = secrets.token_hex(6)
            # Fetch/authenticate the exact distro source before installing any
            # development packages or creating the dedicated builder account.
            downloads, source = self._download(trusted['source_profile'], native, identifier)
            if self.host.inspect() != native or source_profile(self.host) != trusted['source_profile']:
                raise RootActionError('PHP/source identity changed during authenticated fetch.', 'STALE_PLAN')
            if dependencies['changes']:
                with phase_context('build-dependencies'):
                    value = self.host.run(dependencies['command'], timeout=900)
                if value.returncode:
                    raise RootActionError('Build dependency installation failed; package state may have changed.', 'FEATHER_DEPENDENCIES_FAILED')
            installed = self._packages()._installed(sorted(set(dependencies['names']) | {row['name'] for row in dependencies['changes']}))
            if any(name not in installed for name in dependencies['names']) or any(installed.get(row['name'], {}).get('version') != row['candidate'] for row in dependencies['changes']):
                raise RootActionError('Not every reviewed build dependency version is installed.', 'FEATHER_DEPENDENCY_MISMATCH')
            for name, expected in (('php8.5-dev', native['package_version']), ('libsqlite3-dev', native['sqlite_package_version'])):
                if installed.get(name, {}).get('version') != expected:
                    raise RootActionError('Installed development packages do not exactly match PHP/SQLite.', 'FEATHER_DEPENDENCY_MISMATCH')
            for argv, pattern in ((['php-config8.5', '--phpapi'], native['abi']), (['php-config8.5', '--version'], native['php_version'])):
                if self.host.checked(argv).strip() != pattern:
                    raise RootActionError('php-config does not match the installed ABI/version.', 'FEATHER_DEPENDENCY_MISMATCH')
            account = self._ensure_user()
            scratch = Path('/var/tmp')
            info = scratch.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or not info.st_mode & stat.S_ISVTX:
                raise RootActionError('A native sticky root-owned /var/tmp is required.', 'FEATHER_UNSAFE_FILE')
            workspace = Path(tempfile.mkdtemp(prefix='threewa-feather-', dir=scratch))
            os.chown(workspace, account['uid'], account['gid'])
            inputs = workspace / 'input'
            inputs.mkdir(mode=0o755)
            inputs.chmod(0o755)
            for name in [source['dsc'], *[row['name'] for row in source['files']]]:
                (inputs / name).write_bytes(_file(downloads / name, 512 * 1024 * 1024))
                (inputs / name).chmod(0o644)
            job = {'workspace': str(workspace), 'native': native, 'source': source, 'nonce': identifier, 'patch_sha256': PATCH_SHA256}
            job_path = workspace / 'job.json'
            job_path.write_text(_json(job) + '\n', encoding='utf-8')
            job_path.chmod(0o644)
            unit = 'threewa-feather-build-' + identifier + '.service'
        # The orchestrator's FEATHER_BUILDING record remains pending while the
        # unprivileged unit works. Do not retain the global flock for minutes.
        try:
            with phase_context('compile'):
                self.host.checked(unit_command(str(workspace), str(job_path), identifier), timeout=900)
            emit_progress('verify')
            os.chown(workspace, 0, 0)
            workspace.chmod(0o700)
            report = json.loads(self._worker_file(workspace, 'result.json', account['uid']).decode())
            if report.get('verified') is not True or report.get('source') != source or report.get('patch_sha256') != PATCH_SHA256:
                raise RootActionError('Unprivileged build proof failed.', 'FEATHER_BUILD_FAILED')
            module = self._worker_file(workspace, 'output/pdo_sqlite.so', account['uid'])
            probe = self._worker_file(workspace, 'output/featherproof.so', account['uid'])
            if not _elf(module) or not _elf(probe):
                raise RootActionError('Unexpected ELF build artifact.', 'FEATHER_BUILD_FAILED')
            with self.core._lock():
                self.core._check_pending(ignore_id=transaction_id)
                if self.host.inspect() != native:
                    raise RootActionError('Native PHP/SQLite changed during compilation.', 'STALE_PLAN')
                artifact_id = _sha(module + probe + PATCH_SHA256.encode())
                directory = MODULES + '/' + artifact_id
                self._public_dir(directory)
                for name, data in (('pdo_sqlite.so', module), ('featherproof.so', probe)):
                    target = self.host.path(directory + '/' + name)
                    if target.exists():
                        if _file(target) != data:
                            raise RootActionError('Artifact publication collision.', 'FEATHER_ARTIFACT_INVALID')
                    else:
                        self.core._atomic(directory + '/' + name, data, {'mode': 0o644, 'uid': 0, 'gid': 0, 'xattrs': {}})
                self.core._secure_dir(STATE + '/manifests')
                artifact = {key: native[key] for key in ('php_version', 'package_version', 'abi', 'arch', 'nts')}
                artifact.update(module_path=directory + '/pdo_sqlite.so', sha256=_sha(module), probe_so_path=directory + '/featherproof.so',
                                probe_sha256=_sha(probe), source_sha256=source['source_sha256'], patch_sha256=PATCH_SHA256,
                                probe_function='threewa_' + identifier, probe_value='feather-' + identifier,
                                buildmanifest=STATE + '/manifests/' + artifact_id + '.json')
                self.core._private_json(artifact['buildmanifest'], {'schema': 1, 'verified': True, 'artifact': artifact,
                    'source_profile': trusted['source_profile'], 'source': source, 'dependencies': dependencies,
                    'installed_dependencies': installed, 'patch_profile': PATCH_PROFILE, 'build_proof': report})
                validate_artifact(artifact, self.host)
                return artifact
        except Exception as error:
            code = error.code if isinstance(error, RootActionError) else 'FEATHER_BUILD_FAILED'
            raise RootActionError('Feather compilation/verification failed; inspect the private diagnostic at '
                                  + STATE + '/diagnostics/' + identifier + '.json.', code) from None
        finally:
            # RuntimeMaxSec and KillMode also protect a vanished UI process.
            self.host.run(['systemctl', 'stop', unit], timeout=30)
            if workspace.exists():
                os.chown(workspace, 0, 0)
                workspace.chmod(0o700)
                if (workspace / 'error.json').exists():
                    try:
                        diagnostic = json.loads(self._worker_file(workspace, 'error.json', account['uid']).decode())
                        self.core._secure_dir(STATE + '/diagnostics')
                        self.core._private_json(STATE + '/diagnostics/' + identifier + '.json', diagnostic)
                    except Exception:
                        pass
                if workspace.parent == Path('/var/tmp') and workspace.name.startswith('threewa-feather-') and not workspace.is_symlink():
                    shutil.rmtree(workspace)

    def _worker_file(self, workspace, relative, builder_uid):
        current = workspace
        parts = Path(relative).parts
        for index, part in enumerate(parts):
            current = current / part
            info = current.lstat()
            valid = stat.S_ISREG if index == len(parts) - 1 else stat.S_ISDIR
            if (not valid(info.st_mode) or info.st_uid not in {0, builder_uid}
                    or info.st_mode & 0o022 or current.is_symlink()):
                raise RootActionError('Untrusted build output path.', 'FEATHER_UNSAFE_FILE')
        return _file(current, root_owned=False)


_WORKER_DIAGNOSTICS = {'stage': 'preflight', 'commands': []}


def _worker_run(argv, cwd, timeout=180, stage='command'):
    _WORKER_DIAGNOSTICS['stage'] = stage
    binary = resolve(argv[0])
    if not binary:
        raise RootActionError('Missing builder executable.', 'FEATHER_BUILD_FAILED')
    resolved = Path(binary).resolve(strict=True)
    for part in [resolved, *resolved.parents]:
        info = part.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise RootActionError('Builder executable is not root controlled.', 'FEATHER_BUILD_FAILED')
    try:
        result = run_captured([binary, *argv[1:]], timeout=timeout, cwd=cwd, command=argv[0], text=False,
            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': str(cwd), 'LC_ALL': 'C', 'LANG': 'C'})
    except subprocess.TimeoutExpired:
        _WORKER_DIAGNOSTICS['commands'].append({'stage': stage, 'command': argv[0], 'code': 'TIMEOUT'})
        raise RootActionError('Unprivileged build step timed out.', 'TIMEOUT') from None
    _WORKER_DIAGNOSTICS['commands'].append({'stage': stage, 'command': argv[0], 'code': result.returncode,
        'stdout_tail': result.stdout[-8192:].decode('utf-8', 'replace'),
        'stderr_tail': result.stderr[-8192:].decode('utf-8', 'replace')})
    if result.returncode:
        raise RootActionError('Unprivileged build/probe failed; native diagnostics were discarded.', 'FEATHER_BUILD_FAILED')
    if result.output_truncated and stage.startswith('probe-'):
        raise RootActionError('Unprivileged probe metadata exceeded its bound.', 'FEATHER_BUILD_FAILED')
    return result.stdout.decode('utf-8', 'strict')


def _worker(job_path):
    if os.geteuid() == 0:
        raise RootActionError('The compiler worker must be unprivileged.', 'FEATHER_BUILD_FAILED')
    job_path = Path(job_path)
    job = json.loads(_file(job_path, 1024 * 1024).decode())
    workspace = Path(job['workspace'])
    if job_path != workspace / 'job.json' or not re.fullmatch(r'/var/tmp/threewa-feather-[A-Za-z0-9_-]+', str(workspace)):
        raise RootActionError('Unsafe compiler workspace.', 'FEATHER_BUILD_FAILED')
    nonce = job['nonce']
    if not re.fullmatch('[a-f0-9]{12}', nonce) or job['patch_sha256'] != PATCH_SHA256:
        raise RootActionError('Invalid compiler job.', 'FEATHER_BUILD_FAILED')
    source = verify_source_downloads(workspace / 'input', job['native']['package_version'])
    if source != job['source']:
        raise RootActionError('Source bytes changed before extraction.', 'FEATHER_SOURCE_INTEGRITY')
    tree = workspace / 'tree'
    _worker_run(['dpkg-source', '--no-check', '-x', str(workspace / 'input' / source['dsc']), str(tree)], workspace, stage='source-extract')
    extension = tree / 'ext/pdo_sqlite'
    _WORKER_DIAGNOSTICS['stage'] = 'source-patch'
    before = {name: _file(extension / name, 1024 * 1024, root_owned=False) for name in ('pdo_sqlite.c', 'sqlite_driver.c')}
    patched = patch_sources(before)
    for name, data in patched.items():
        (extension / name).write_bytes(data)
    _worker_run(['phpize8.5'], extension, stage='phpize')
    _worker_run(['sh', './configure', '--with-php-config=/usr/bin/php-config8.5', '--with-pdo-sqlite'], extension, stage='configure')
    _worker_run(['make', '-j2'], extension, timeout=600, stage='make')
    output = workspace / 'output'
    output.mkdir(mode=0o700)
    shutil.copyfile(extension / 'modules/pdo_sqlite.so', output / 'pdo_sqlite.so')
    function, value = 'threewa_' + nonce, 'feather-' + nonce
    c_source = ('#include <sqlite3ext.h>\nSQLITE_EXTENSION_INIT1\n'
        'static void proof(sqlite3_context *c,int n,sqlite3_value **v){sqlite3_result_text(c,"' + value + '",-1,SQLITE_STATIC);}\n'
        'int sqlite3_featherproof_init(sqlite3 *db,char **error,const sqlite3_api_routines *api){SQLITE_EXTENSION_INIT2(api);'
        'return sqlite3_create_function(db,"' + function + '",0,SQLITE_UTF8|SQLITE_DETERMINISTIC,0,proof,0,0);}\n')
    (output / 'proof.c').write_text(c_source)
    _worker_run(['cc', '-fPIC', '-shared', '-o', str(output / 'featherproof.so'), str(output / 'proof.c')], output, stage='probe-compile')
    php = r'''$d=new PDO("sqlite::memory:");$d->setAttribute(PDO::ATTR_ERRMODE,PDO::ERRMODE_EXCEPTION);$d->exec("CREATE TABLE t(v TEXT)");$s=$d->prepare("INSERT INTO t VALUES (?)");$s->execute(["羽山"]);if($d->query("SELECT v FROM t")->fetchColumn()!=="羽山")exit(21);$attempt=ini_set("pdo_sqlite.feather_sql_load_extension","1");$loaded=false;try{$s=$d->prepare("SELECT load_extension(?)");$s->execute([$argv[1]]);$loaded=true;}catch(Throwable $e){}$value=null;if($loaded){$value=$d->query("SELECT PROBE_FUNCTION()")->fetchColumn();}echo json_encode(["loaded"=>$loaded,"value"=>$value,"ini_set_rejected"=>$attempt===false,"unicode"=>true]);'''.replace('PROBE_FUNCTION', function)
    proofs = {}
    for label, module_path, enabled in (('custom_on', str(output / 'pdo_sqlite.so'), True),
                                        ('custom_off', str(output / 'pdo_sqlite.so'), False),
                                        ('stock', job['native']['original_module'], False)):
        argv = ['php8.5', '-n', '-d', 'extension=' + job['native']['pdo_module'], '-d', 'extension=' + module_path,
                '-d', GATE + '=' + ('1' if enabled else '0'), '-r', php, '--', str(output / 'featherproof.so')]
        proof = json.loads(_worker_run(argv, output, stage='probe-' + label))
        if proof.get('loaded') is not enabled or not proof.get('unicode') or (enabled and proof.get('value') != value) or (label == 'custom_off' and not proof.get('ini_set_rejected')):
            raise RootActionError('SQL load-extension gate or Unicode probe failed.', 'FEATHER_PROBE_FAILED')
        proofs[label] = proof
    result = {'verified': True, 'source': source, 'patch_sha256': PATCH_SHA256,
              'source_before': {name: _sha(data) for name, data in before.items()},
              'source_after': {name: _sha(data) for name, data in patched.items()},
              'probe_c_sha256': _sha(c_source.encode()), 'probes': proofs}
    (workspace / 'result.json').write_text(_json(result) + '\n')


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--worker':
        raise SystemExit('Only the private unprivileged build worker may execute this module directly.')
    try:
        _worker(sys.argv[2])
    except Exception as error:
        try:
            workspace = Path(sys.argv[2]).parent
            if (re.fullmatch(r'/var/tmp/threewa-feather-[A-Za-z0-9_-]+', str(workspace))
                    and workspace.stat().st_uid == os.geteuid() and not workspace.is_symlink()):
                _WORKER_DIAGNOSTICS['error_code'] = error.code if isinstance(error, RootActionError) else type(error).__name__
                (workspace / 'error.json').write_text(_json(_WORKER_DIAGNOSTICS) + '\n')
        except Exception:
            pass
        print('Unprivileged Feather compilation or verification failed.', file=sys.stderr)
        raise SystemExit(1)

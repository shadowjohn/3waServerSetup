"""Narrow Ubuntu recipes; the transaction manager owns configuration writes.

Only the declared fields are managed. Runtime checks report their actual scope,
and never turn a parser check into an FTP login, share or application test.
"""
import hashlib
import os
import re
import shlex
import stat

from .model import FIELDS, preview


_PACKAGES = {'apache': ['apache2'], 'php': ['php-fpm', 'php-cli', 'php-sqlite3'],
             'vsftpd': ['vsftpd'], 'samba': ['samba'], 'mariadb': ['mariadb-server'],
             'ssh': ['openssh-server'], 'time': ['ntpsec-ntpdate']}
_UNITS = {'apache': 'apache2.service', 'vsftpd': 'vsftpd.service',
          'samba': 'smbd.service', 'mariadb': 'mariadb.service'}
_MARKER = '# Managed by 3waServerSetup; selected settings only.'
_APACHE_AVAILABLE = '/etc/apache2/conf-available/zz-3wa-server-setup.conf'
_APACHE_ENABLED = '/etc/apache2/conf-enabled/zz-3wa-server-setup.conf'
_MYSQL_DROPIN = '/etc/mysql/mariadb.conf.d/99-3wa-server-setup.cnf'
_SSH_DROPIN = '/etc/ssh/sshd_config.d/00-3wa-server-setup.conf'
_SCOPES = {
    'apache': 'Managed global Apache directives, native include, configtest and daemon state; HTTP/vhost behavior not checked.',
    'php': 'Selected CLI ini values and SQLite modules, plus an independent local FPM FastCGI request when FPM is active; Apache/HTTP application routing not checked.',
    'vsftpd': 'Selected config structure and daemon state only; authentication/login, TLS, data transfers and passive connectivity not checked.',
    'samba': 'testparm effective selected globals and smbd daemon state; share access, credentials and filesystem permissions not checked.',
    'mariadb': 'Server option-file validation and selected GLOBAL variables over local socket; application accounts, queries, data and remote access not checked.',
    'ssh': 'Selected global sshd configuration and native syntax checked; only an already active SSH daemon is reloaded. Existing sessions and per-user Match behavior are not verified.',
    'time': 'Current timezone only; NTP synchronization is observed separately and is not guaranteed.',
}


class PHPVerificationError(ValueError):
    """Only validated configuration identifiers may survive transaction rollback."""
    def __init__(self, scope, issue, key=None, source=None):
        messages = {'sapi': 'probe did not return the expected SAPI',
                    'loaded-ini': 'loaded php.ini differs from the matching native target',
                    'scanned-ini': 'managed late ini is not loaded in the request scanned files',
                    'extensions': 'SQLite extensions are not loaded',
                    'value': 'effective value differs from the selected target'}
        if scope not in {'cli', 'fpm'} or issue not in messages or key is not None and key not in FIELDS['php']:
            raise ValueError('Invalid PHP verification diagnostic identifier.')
        detail = 'PHP ' + scope.upper() + (' ' + key if key else '') + ': ' + messages[issue] + '.'
        if isinstance(source, str) and re.fullmatch(
                r'/etc/php/[0-9]+\.[0-9]+/(?:cli|fpm)/(?:php\.ini|conf\.d/[A-Za-z0-9_.-]+\.ini|pool\.d/[A-Za-z0-9_.-]+\.conf)', source):
            detail += ' Inspect override source ' + source + '.'
        self.safe_detail = detail
        super().__init__(detail)


class SSHVerificationError(ValueError):
    """Only the two known SSH keys and fixed configuration paths are exposed."""
    def __init__(self, key):
        if key not in FIELDS['ssh']:
            raise ValueError('Unknown SSH keepalive verification key.')
        self.safe_detail = ('SSH global ' + key + ' differs from the selected target; inspect '
                            '/etc/ssh/sshd_config and earlier drop-ins for first-obtained precedence. '
                            'Per-user Match behavior remains unverified.')
        super().__init__(self.safe_detail)


def packages(module):
    return list(_PACKAGES.get(module, []))


def _run(host, argv, timeout=30):
    result = host.run(argv, timeout=timeout)
    if result.returncode:
        # Native diagnostics may contain paths, configuration or credentials.
        raise ValueError(f'{argv[0]} check failed (exit {result.returncode}); inspect native service diagnostics locally.')
    return result.stdout or ''


def _properties(text):
    return dict(line.split('=', 1) for line in text.splitlines() if '=' in line)


def _service(host, unit):
    if not unit:
        return {'unit': None, 'active': None, 'enabled': None}
    result = _properties(_run(host, ['systemctl', 'show', unit, '--no-pager',
                                    '--property=LoadState', '--property=ActiveState', '--property=UnitFileState']))
    if result.get('LoadState') not in {'loaded', 'not-found', 'masked'}:
        raise ValueError('Cannot reliably determine service state.')
    active = result.get('ActiveState')
    if active not in {'active', 'inactive', 'failed'}:
        raise ValueError('Service is changing state; retry after it settles.')
    enabled = result.get('UnitFileState', '')
    if enabled not in {'enabled', 'enabled-runtime', 'disabled', 'masked', 'masked-runtime', 'static', 'indirect', ''}:
        raise ValueError('Unknown service enablement state; no state changes planned.')
    return {'unit': unit, 'active': active == 'active', 'enabled': enabled in {'enabled', 'enabled-runtime'},
            'load_state': result.get('LoadState'), 'unit_file_state': enabled}


def _installed(host, module):
    installed = []
    for name in packages(module):
        result = host.run(['dpkg-query', '-W', '-f=${Status}\n', name], timeout=10)
        if result.returncode == 0 and (result.stdout or '').strip() == 'install ok installed':
            installed.append(name)
    return installed


def _read_required(host, path, exact=False):
    text = host.read(path)
    if text is None:
        raise ValueError(f'Unsupported layout: required configuration missing: {path}')
    # read() performs the host's regular-file/path trust check. Native files
    # that we edit need their original line endings, unlike read_text().
    return host.path(path).read_bytes().decode('utf-8') if exact else text


def _ini_values(text, allowed):
    values = {}
    for line in text.splitlines():
        match = re.match(r'^\s*([^#;=]+?)\s*=\s*(.*?)\s*$', line)
        if match and match[1].strip() in allowed:
            values[match[1].strip()] = match[2].split(';', 1)[0].strip().strip('"\'')
    return values


def _apache_layout(host):
    text = _read_required(host, '/etc/apache2/apache2.conf')
    if not re.search(r'^\s*Include(?:Optional)?\s+["\']?(?:/etc/apache2/)?conf-enabled/\*\.conf["\']?\s*(?:#.*)?$', text, re.M):
        raise ValueError('Unsupported Apache layout: native conf-enabled/*.conf include is missing.')


def _trusted_fpm_path(host, path, symlink=False):
    """Native module links are read exceptions, never transaction write targets."""
    info = path.lstat()
    if symlink:
        if not stat.S_ISLNK(info.st_mode):
            raise ValueError('FPM module link changed during inspection.')
        if os.name == 'posix' and info.st_uid != 0:
            raise ValueError('FPM module symlink must be root owned.')
        current = path.parent
    else:
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('FPM configuration must be a regular file.')
        if info.st_size > 1024 * 1024:
            raise ValueError('FPM configuration exceeds the bounded inspection size.')
        current = path
    for item in [current, *current.parents]:
        info = item.stat()
        if os.name == 'posix' and (info.st_uid != 0 or info.st_mode & 0o022):
            raise ValueError('FPM configuration and parents must be root controlled and non-user-writable.')
        if item == host.root:
            break


def _no_fpm_bootstrap(text, pool=False):
    name = r'auto_(?:prepend|append)_file'
    key = r'php_(?:admin_)?(?:value|flag)\[\s*' + name + r'\s*\]' if pool else name
    for match in re.finditer(r'^[ \t]*(' + key + r')[ \t]*=[ \t]*([^\r\n]*)', text, re.M | re.I):
        # Only explicit empty values are safe. Unknown interpolation and a
        # bootstrap later overridden to empty still require manual inspection.
        if not re.fullmatch(r'\s*(?:""|\'\')?\s*(?:;[^\r\n]*)?', match[2]):
            raise ValueError('FPM application bootstrap (auto_prepend_file/auto_append_file) is configured; independent probes are refused without changing it.')


def _php_ini_inputs(host, version, scope):
    base = f'/etc/php/{version}/{scope}'
    ini_path = host.path(base + '/php.ini')
    _trusted_fpm_path(host, ini_path)
    main = _read_required(host, base + '/php.ini')
    _no_fpm_bootstrap(main)
    inputs = [(base + '/php.ini', main)]
    directory = host.path(base + '/conf.d')
    if not directory.is_dir():
        raise ValueError(f'Unsupported PHP {scope.upper()} ini scan directory.')
    for entry in sorted(directory.glob('*.ini')):
        virtual = base + '/conf.d/' + entry.name
        if entry.is_symlink():
            _trusted_fpm_path(host, entry, symlink=True)
            target = entry.readlink().as_posix()
            match = re.fullmatch(r'(?:\.\./\.\./mods-available/|/etc/php/' + re.escape(version)
                                 + r'/mods-available/)([A-Za-z0-9_.-]+\.ini)', target)
            if not match:
                raise ValueError('Unsupported FPM conf.d symlink target; only native same-version mods-available links are accepted.')
            virtual = f'/etc/php/{version}/mods-available/' + match[1]
        canonical = host.path(virtual)
        _trusted_fpm_path(host, canonical)
        text = _read_required(host, virtual)
        _no_fpm_bootstrap(text)
        inputs.append((base + '/conf.d/' + entry.name, text))
    return inputs


def _fpm_bootstrap_guard(host, version, pool_text):
    _php_ini_inputs(host, version, 'fpm')
    _no_fpm_bootstrap(pool_text, pool=True)


def _cli(host, version):
    _php_ini_inputs(host, version, 'cli')
    return {'version': version, 'binary': 'php' + version,
            'main_ini': f'/etc/php/{version}/cli/php.ini',
            'ini': f'/etc/php/{version}/cli/conf.d/zz-3wa-server-setup.ini'}


def _php_configuration(host, version, scope):
    """配置來源供檢閱；真正生效值仍須分別執行 CLI／FPM probe。"""
    values, sources = {}, {}
    for path, text in _php_ini_inputs(host, version, scope):
        for key, value in _ini_values(text, FIELDS['php']).items():
            values[key] = value
            sources[key] = path
    if scope == 'fpm':
        for path in sorted(host.path(f'/etc/php/{version}/fpm/pool.d').glob('*.conf')):
            virtual = f'/etc/php/{version}/fpm/pool.d/' + path.name
            text = _read_required(host, virtual)
            matches = re.finditer(r'^\s*php_(?:admin_)?(?:value|flag)\[\s*([^\]]+?)\s*\]\s*=\s*(.*?)\s*$', text, re.M)
            for match in matches:
                key = match[1].strip()
                if key in FIELDS['php']:
                    values[key] = match[2].split(';', 1)[0].strip().strip('"\'')
                    sources[key] = virtual
    return values, sources


def _fpm_unit_layout(host, target):
    properties = ('DropInPaths', 'EnvironmentFiles', 'Environment', 'PassEnvironment',
                  'RootDirectory', 'RootImage', 'ExecStart')
    observed = _properties(_run(host, ['systemctl', 'show', target['unit'], '--no-pager', '--all',
                                      *['--property=' + key for key in properties]]))
    # systemctl's EnvironmentFiles a(sb) printer emits one row per entry;
    # an empty array emits no row even with --all. Verified on Ubuntu 26.04
    # against the native D-Bus a(sb) 0 value; all other keys remain required.
    observed.setdefault('EnvironmentFiles', '')
    # Raw environment values can contain credentials; never include them in a
    # plan, observation, error message or journal.
    if not set(properties).issubset(observed) or any(observed.get(key) for key in properties if key != 'ExecStart'):
        raise ValueError('Unsupported custom FPM systemd unit environment/filesystem layout; independent probes are refused.')
    command = observed.get('ExecStart', '')
    executables = re.findall(r'(?:^|[;{\s])path=([^;\s]+)\s*;', command)
    arguments = re.findall(r'argv\[\]=(.*?)\s*;', command)
    binary = '/usr/sbin/' + target['binary']
    expected = [binary, '--nodaemonize', '--fpm-config',
                f"/etc/php/{target['version']}/fpm/php-fpm.conf"]
    if executables != [binary] or len(arguments) != 1 or arguments[0].split() != expected:
        raise ValueError('Unsupported FPM systemd ExecStart; only the matching native binary and configuration are accepted.')
    manager_environment = _run(host, ['systemctl', 'show-environment'])
    if re.search(r'^[ \t]*(?:export[ \t]+)?[\'"]?(?:PHPRC|PHP_INI_SCAN_DIR)=', manager_environment, re.M):
        raise ValueError('Unsupported PHP environment in the systemd manager; independent probes are refused.')


def _fpm(host):
    parent = host.path('/etc/php')
    candidates = []
    if parent.is_dir():
        for version in parent.iterdir():
            if re.fullmatch(r'[0-9]+\.[0-9]+', version.name):
                ini = f'/etc/php/{version.name}/fpm/php.ini'
                if host.path(ini).is_file():
                    candidates.append(version.name)
    if len(candidates) != 1:
        raise ValueError('Multiple or missing FPM versions; explicit supported target selection is required.')
    version = candidates[0]
    base = f'/etc/php/{version}/fpm'
    _trusted_fpm_path(host, host.path(base + '/php-fpm.conf'))
    config = _read_required(host, base + '/php-fpm.conf')
    includes = re.findall(r'^\s*include\s*=\s*([^;\r\n]+)', config, re.M | re.I)
    if [item.strip() for item in includes] != [base + '/pool.d/*.conf']:
        raise ValueError('Unsupported FPM pool include layout.')
    directory = host.path(base + '/pool.d')
    if not directory.is_dir():
        raise ValueError('Missing FPM pool directory.')
    pools = []
    for file in sorted(directory.glob('*.conf')):
        _trusted_fpm_path(host, host.path(base + '/pool.d/' + file.name))
        text = _read_required(host, base + '/pool.d/' + file.name)
        sections = re.findall(r'^\s*\[([^\]]+)\]', text, re.M)
        if len(sections) != 1 or sections[0] == 'global' or re.search(r'^\s*include\s*=', text, re.M | re.I):
            raise ValueError('Unsupported FPM pool layout.')
        pools.append((sections[0], text))
    if len(pools) != 1:
        raise ValueError('Multiple or missing FPM pools; refusing to guess the target pool.')
    name, text = pools[0]
    selected = _ini_values(text, {'listen', 'user', 'group', 'chroot'})
    socket_path = selected.get('listen', '')
    if (not re.fullmatch(r'/run/php/[a-zA-Z0-9_.-]+\.sock', socket_path)
            or selected.get('user') != 'www-data' or selected.get('chroot')):
        raise ValueError('Unsupported FPM pool: requires www-data and a local /run/php Unix socket without chroot.')
    if not host.path(base + '/conf.d').is_dir():
        raise ValueError('Unsupported FPM ini scan directory.')
    _fpm_bootstrap_guard(host, version, text)
    target = {'version': version, 'unit': f'php{version}-fpm.service', 'socket': socket_path,
              'pool': name, 'ini': base + '/conf.d/zz-3wa-server-setup.ini', 'binary': 'php-fpm' + version}
    _fpm_unit_layout(host, target)
    return target


def _mysql_layout(host):
    # Ubuntu uses alternatives links for my.cnf. Only this fixed chain is
    # accepted for a read; no transaction target is allowed to be a symlink.
    native = host.path('/etc/mysql') / 'my.cnf'
    if native.is_symlink():
        first = str(native.readlink())
        if first != '/etc/alternatives/my.cnf':
            raise ValueError('Unsupported MariaDB my.cnf symlink target.')
        alternative = host.path('/etc/alternatives') / 'my.cnf'
        if not alternative.is_symlink() or str(alternative.readlink()) != '/etc/mysql/mariadb.cnf':
            raise ValueError('Unsupported MariaDB alternatives configuration chain.')
        text = _read_required(host, '/etc/mysql/mariadb.cnf')
    else:
        text = _read_required(host, '/etc/mysql/my.cnf')
    if not re.search(r'^\s*!includedir\s+/etc/mysql/mariadb\.conf\.d/?\s*$', text, re.M):
        raise ValueError('Unsupported MariaDB layout: native mariadb.conf.d include is missing.')


def _ssh_input(host, virtual):
    path = host.path(virtual)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
        raise ValueError('SSH configuration must be a bounded native regular file.')
    for item in [path, *path.parents]:
        metadata = item.lstat()
        if os.name == 'posix' and (metadata.st_uid != 0 or metadata.st_mode & 0o022):
            raise ValueError('SSH configuration files and parents must be root controlled.')
        if item == host.root:
            break
    return _read_required(host, virtual)


def _ssh_tokens(text, shell_assignments=False):
    for line in text.splitlines():
        try:
            if not shell_assignments:
                # OpenSSH also accepts Keyword=value; do not let it hide Include/Match.
                line = re.sub(r'^(\s*[A-Za-z][A-Za-z0-9]*)\s*=\s*', r'\1 ', line, count=1)
            parts = shlex.split(line, comments=True)
        except ValueError:
            raise ValueError('Unsupported SSH configuration quoting; inspect the native configuration locally.') from None
        if parts:
            yield parts


def _ssh_unit_layout(host):
    properties = ('DropInPaths', 'EnvironmentFiles', 'Environment', 'PassEnvironment',
                  'RootDirectory', 'RootImage', 'ExecStart', 'ExecReload')
    raw = _run(host, ['systemctl', 'show', 'ssh.service', '--no-pager', '--all',
                     *['--property=' + key for key in properties]])
    rows = {}
    for line in raw.splitlines():
        key, separator, value = line.partition('=')
        if separator and key in properties:
            rows.setdefault(key, []).append(value)
    # systemctl 將 ExecReload array 的每個命令各印一行，不能用 dict 留最後一行。
    if any(len(values) != 1 for key, values in rows.items() if key != 'ExecReload'):
        raise ValueError('Duplicate SSH unit properties need separate native-layout integration.')
    observed = {key: '\n'.join(values) for key, values in rows.items()}
    observed.setdefault('EnvironmentFiles', '')
    environment_file = observed['EnvironmentFiles'].strip()
    if (not set(properties).issubset(observed)
            or any(observed[key] for key in properties if key not in {'EnvironmentFiles', 'ExecStart', 'ExecReload'})
            or environment_file and not re.fullmatch(r'/etc/default/ssh\s+\(ignore_errors=yes\)', environment_file)):
        raise ValueError('Custom SSH systemd environment or filesystem layout needs separate integration.')
    command = observed['ExecStart']
    binaries = re.findall(r'(?:^|[;{\s])path=([^;\s]+)\s*;', command)
    arguments = re.findall(r'argv\[\]=(.*?)\s*;', command)
    if (binaries != ['/usr/sbin/sshd'] or len(arguments) != 1
            or arguments[0].split() not in (['/usr/sbin/sshd', '-D'], ['/usr/sbin/sshd', '-D', '$SSHD_OPTS'])):
        raise ValueError('Custom SSH ExecStart or daemon options need separate integration.')
    reload = observed['ExecReload']
    reload_binaries = re.findall(r'(?:^|[;{\s])path=([^;\s]+)\s*;', reload)
    reload_arguments = re.findall(r'argv\[\]=(.*?)\s*;', reload)
    if (reload_binaries != ['/usr/sbin/sshd', '/bin/kill']
            or [item.split() for item in reload_arguments] != [
                ['/usr/sbin/sshd', '-t'], ['/bin/kill', '-HUP', '$MAINPID']]):
        raise ValueError('Custom SSH ExecReload is refused; native syntax-check then HUP reload is required.')
    environment = _run(host, ['systemctl', 'show-environment'])
    if any(value.strip() for value in re.findall(r'^SSHD_OPTS=(.*)$', environment, re.M)):
        raise ValueError('Systemd manager SSHD_OPTS must be empty before native keepalive integration.')
    return {'exec_start': arguments[0].split(), 'exec_reload': [item.split() for item in reload_arguments],
            'native_environment_file': bool(environment_file)}


def _ssh_layout(host):
    unit_layout = _ssh_unit_layout(host)
    main = '/etc/ssh/sshd_config'
    content = _ssh_input(host, main)
    inputs = {main: hashlib.sha256(host.path(main).read_bytes()).hexdigest()}
    global_scope, includes = True, 0
    for parts in _ssh_tokens(content):
        key = parts[0].lower()
        if key == 'match':
            global_scope = False
        elif key == 'include':
            if not global_scope or parts[1:] != ['/etc/ssh/sshd_config.d/*.conf'] or includes:
                raise ValueError('SSH requires one native global Include before Match; custom or conditional Include needs separate integration.')
            includes += 1
    if includes != 1:
        raise ValueError('SSH native global Include /etc/ssh/sshd_config.d/*.conf is missing.')
    directory = host.path('/etc/ssh/sshd_config.d')
    if not directory.is_dir():
        raise ValueError('SSH native drop-in directory is missing.')
    for item in [directory, *directory.parents]:
        metadata = item.lstat()
        if os.name == 'posix' and (metadata.st_uid != 0 or metadata.st_mode & 0o022):
            raise ValueError('SSH drop-in directory and parents must be root controlled.')
        if item == host.root:
            break
    entries = sorted(directory.glob('*.conf'))
    if len(entries) > 256:
        raise ValueError('SSH drop-in inventory exceeds its inspection limit.')
    total = 0
    for entry in entries:
        virtual = '/etc/ssh/sshd_config.d/' + entry.name
        text = _ssh_input(host, virtual)
        total += len(text.encode('utf-8'))
        if total > 4 * 1024 * 1024:
            raise ValueError('SSH drop-in inventory exceeds its inspection limit.')
        for parts in _ssh_tokens(text):
            if parts[0].lower() == 'include':
                raise ValueError('Nested SSH Include needs separate integration before managing keepalive.')
            if parts[0].lower() == 'match' and entry.name < _SSH_DROPIN.rsplit('/', 1)[1]:
                raise ValueError('An earlier SSH drop-in enters Match before the managed global settings.')
        inputs[virtual] = hashlib.sha256(host.path(virtual).read_bytes()).hexdigest()
    defaults = '/etc/default/ssh'
    if host.path(defaults).exists():
        text = _ssh_input(host, defaults)
        for parts in _ssh_tokens(text, shell_assignments=True):
            if parts not in (['SSHD_OPTS='], ['SSHD_OPTS=""'], ["SSHD_OPTS=''"]):
                if not (len(parts) == 1 and re.fullmatch(r'SSHD_OOM_ADJUST=-?[0-9]+', parts[0])):
                    raise ValueError('Custom SSH daemon defaults or SSHD_OPTS need separate integration; native options were preserved.')
        inputs[defaults] = hashlib.sha256(host.path(defaults).read_bytes()).hexdigest()
    service = _service(host, 'ssh.service')
    if service['load_state'] != 'loaded':
        raise ValueError('The native SSH service unit is unavailable; its state was preserved.')
    return {'inputs': inputs, 'ssh_service': service, 'unit_layout': unit_layout}


def _ssh_effective(host):
    selected = {'clientaliveinterval': 'ClientAliveInterval', 'tcpkeepalive': 'TCPKeepAlive'}
    result = {}
    for line in _run(host, ['sshd', '-T']).splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in selected:
            name = selected[parts[0].lower()]
            if name in result:
                raise ValueError('SSH returned duplicate global keepalive evidence.')
            result[name] = parts[1].strip()
    if set(result) != set(FIELDS['ssh']):
        raise ValueError('SSH global keepalive evidence is incomplete.')
    return result


def verify_ssh_configuration(host, plan):
    """RootManager 在 reload 前執行；不以 -o 強制目標值取得通過。"""
    _ssh_layout(host)
    effective = _ssh_effective(host)
    for key, expected in plan['values'].items():
        if not _same(key, effective.get(key), expected):
            raise SSHVerificationError(key)
    return effective


def capabilities(host, module):
    result = {'packages': packages(module), 'installed': False, 'installed_packages': [],
              'supported': module in _PACKAGES, 'detail': '', 'configured': {}, 'unit': _UNITS.get(module),
              'active': None, 'enabled': None}
    if module not in _PACKAGES:
        result['detail'] = 'This page has no supported root write recipe.'
        return result
    try:
        installed = _installed(host, module)
        result.update(installed_packages=installed, installed=len(installed) == len(packages(module)))
        if not result['installed']:
            result['detail'] = 'Install the selected distribution packages before applying settings.'
            return result
        if module == 'php':
            target = _fpm(host)
            result['unit'] = target['unit']
            result['fpm'] = target
            result['cli'] = _cli(host, target['version'])
            result['configured_by_sapi'], result['configuration_sources'] = {}, {}
            for scope in ('cli', 'fpm'):
                configured, sources = _php_configuration(host, target['version'], scope)
                result['configured_by_sapi'][scope] = configured
                result['configuration_sources'][scope] = sources
            result['configured'] = result['configured_by_sapi']['fpm']
        elif module == 'apache':
            _apache_layout(host)
            result['configured'] = dict(re.findall(r'^\s*(ServerTokens|ServerSignature)\s+(\S+)', host.read(_APACHE_AVAILABLE) or '', re.M))
        elif module == 'vsftpd':
            result['configured'] = _ini_values(_read_required(host, '/etc/vsftpd.conf'), FIELDS[module])
        elif module == 'samba':
            result['configured'] = _samba_values(_read_required(host, '/etc/samba/smb.conf'))
        elif module == 'mariadb':
            _mysql_layout(host)
            result['configured'] = _ini_values(host.read(_MYSQL_DROPIN) or '', FIELDS[module])
        elif module == 'ssh':
            result['ssh_layout'] = _ssh_layout(host)
            result['ssh_service'] = result['ssh_layout']['ssh_service']
            result['configured'] = _ssh_effective(host)
        elif module == 'time':
            observed = _properties(_run(host, ['timedatectl', 'show', '--property=Timezone', '--property=NTPSynchronized']))
            result['configured'] = {key: observed[key] for key in FIELDS[module] if key in observed}
        snapshot = _service(host, result['unit'])
        result.update(service_snapshot=snapshot, active=snapshot['active'], enabled=snapshot['enabled'])
        result['detail'] = 'Observed configuration only; use apply verification for the stated health scope.'
    except (ValueError, OSError) as error:
        result.update(supported=False, detail=str(error))
    return result


def _file(host, path, content, owned=False):
    before = host.read(path)
    if owned and before is not None and not before.startswith((_MARKER + '\n', _MARKER.replace('#', ';', 1) + '\n')):
        raise ValueError(f'Existing tool path has unknown ownership/content: {path}')
    return {'path': path, 'before_sha256': hashlib.sha256(host.path(path).read_bytes()).hexdigest() if before is not None else None,
            'content': content, 'mode': 0o644}


def _ftp(host, values):
    text = _read_required(host, '/etc/vsftpd.conf', exact=True)
    newline = '\r\n' if '\r\n' in text else '\n'
    kept = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            kept.append(line)
            continue
        if not re.fullmatch(r'[a-zA-Z0-9_]+=[^\r\n]*', stripped):
            raise ValueError('Unsupported vsftpd structure: expected key=value configuration.')
        if stripped.split('=', 1)[0] not in values:
            kept.append(line)
    prefix = ''.join(kept)
    if prefix and not prefix.endswith(('\n', '\r')):
        prefix += newline
    return prefix + ''.join(f'{key}={value}' + newline for key, value in values.items())


def _samba_values(text):
    result, section = {}, 'global'
    for line in text.splitlines():
        match = re.match(r'^\s*\[([^\]]+)\]', line)
        if match:
            section = match[1].lower()
        elif section == 'global':
            result.update(_ini_values(line, FIELDS['samba']))
    return result


def _samba(host, values):
    text = _read_required(host, '/etc/samba/smb.conf', exact=True)
    newline = '\r\n' if '\r\n' in text else '\n'
    lines = text.splitlines(keepends=True)
    headers = [(index, re.match(r'^\s*\[([^\]]+)\]', line)) for index, line in enumerate(lines)]
    globals_ = [index for index, match in headers if match and match[1].lower() == 'global']
    if len(globals_) != 1:
        raise ValueError('Unsupported Samba layout: requires exactly one explicit [global] section.')
    start = globals_[0] + 1
    end = next((index for index, match in headers if match and index >= start), len(lines))
    kept = []
    for line in lines[start:end]:
        match = re.match(r'^\s*([^#;=]+?)\s*=', line)
        if not match or match[1].strip().lower() not in values:
            kept.append(line)
    prefix = ''.join(lines[:start] + kept)
    if prefix and not prefix.endswith(('\n', '\r')):
        prefix += newline
    return prefix + ''.join(f'\t{key} = {value}' + newline for key, value in values.items()) + ''.join(lines[end:])


def build_plan(host, module, values, service_active=None, service_enabled=None):
    if module not in _PACKAGES or not isinstance(values, dict) or set(values) != set(FIELDS[module]):
        raise ValueError('Unsupported module or incomplete/unknown recipe fields.')
    if any(value is not None and type(value) is not bool for value in (service_active, service_enabled)):
        raise ValueError('Service targets must be bool or None.')
    if module == 'time' and any(value is not None for value in (service_active, service_enabled)):
        raise ValueError('Time recipe does not change NTP service state.')
    if module == 'ssh' and any(value is not None for value in (service_active, service_enabled)):
        raise ValueError('SSH keepalive never changes service active or boot state; start/stop/enable/disable targets are refused.')
    preview(module, values, {})
    values = {key: values[key] for key in FIELDS[module]}
    capability = capabilities(host, module)
    if not capability['supported'] or not capability['installed']:
        raise ValueError(capability['detail'])
    snapshot = capability['service_snapshot']
    if snapshot.get('load_state') == 'not-found':
        raise ValueError('Service unit is missing; repair the distribution installation first.')
    if snapshot['unit'] and snapshot.get('unit_file_state') not in {'enabled', 'disabled'}:
        raise ValueError('Service enablement state is not exactly reversible; only enabled/disabled units are supported.')
    plan = {'schema': 1, 'module': module, 'values': values, 'service_active': service_active,
            'service_enabled': service_enabled, 'files': [], 'validate': [], 'apply_commands': [],
            'restore_commands': [], 'observations': {}, 'service_snapshot': snapshot,
            'notes': [], 'health_scope': _SCOPES[module]}
    if module == 'apache':
        content = _MARKER + '\n' + ''.join(f'{key} {value}\n' for key, value in values.items())
        included = _MARKER + '\nInclude ' + _APACHE_AVAILABLE + '\n'
        plan['files'] = [_file(host, _APACHE_AVAILABLE, content, True), _file(host, _APACHE_ENABLED, included, True)]
        plan['validate'] = [['apache2ctl', 'configtest']]
    elif module == 'php':
        target = capability['fpm']
        content = _MARKER.replace('#', ';', 1) + '\n' + ''.join(f'{key} = {value}\n' for key, value in values.items())
        plan['files'] = [_file(host, target['ini'], content, True),
                         _file(host, capability['cli']['ini'], content, True)]
        plan['validate'] = [[target['binary'], '-t']]
        plan['observations']['fpm'] = target
        plan['observations']['cli'] = capability['cli']
        plan['observations']['configuration_sources'] = capability['configuration_sources']
        plan['notes'].append('Selected settings apply to matching CLI and FPM conf.d drop-ins; native php.ini, Feather activation and SQLite module settings are preserved. CLI and FPM runtime values are verified separately.')
    elif module == 'vsftpd':
        plan['files'] = [_file(host, '/etc/vsftpd.conf', _ftp(host, values))]
        plan['notes'].append('vsftpd has no reliable general offline syntax check; structure plus controlled daemon restart only.')
    elif module == 'samba':
        plan['files'] = [_file(host, '/etc/samba/smb.conf', _samba(host, values))]
        plan['validate'] = [['testparm', '--suppress-prompt', '/etc/samba/smb.conf']]
    elif module == 'mariadb':
        content = _MARKER + '\n[mariadbd]\n' + ''.join(f'{key} = {value}\n' for key, value in values.items())
        plan['files'] = [_file(host, _MYSQL_DROPIN, content, True)]
        plan['validate'] = [['mariadbd', '--defaults-file=/etc/mysql/my.cnf', '--verbose', '--help']]
        plan['notes'].append('Changes restart MariaDB; no credentials, bind-address, grants or data are managed.')
    elif module == 'ssh':
        content = _MARKER + '\n' + ''.join(f'{key} {value}\n' for key, value in values.items())
        change = _file(host, _SSH_DROPIN, content, True)
        plan['files'] = [change]
        plan['validate'] = [['sshd', '-t']]
        plan['observations']['ssh_layout'] = capability['ssh_layout']
        if capability['ssh_service']['active']:
            if change['before_sha256'] != hashlib.sha256(content.encode('utf-8')).hexdigest():
                plan['apply_commands'] = [['systemctl', 'reload', 'ssh.service']]
            # generic restore 先執行 restore_commands，因此先 -t 再送 reload。
            plan['restore_commands'] = [['sshd', '-t'], ['systemctl', 'reload', 'ssh.service']]
        plan['notes'].append('Only global ClientAliveInterval and TCPKeepAlive are managed in a native early drop-in. Authentication, port, keys, Match blocks and SSH service/boot state are preserved; an active daemon receives reload only. Existing sessions and per-user Match values are not verified.')
    else:
        previous = capability['configured'].get('Timezone')
        available = _run(host, ['timedatectl', 'list-timezones']).splitlines()
        if not previous or previous not in available or values['Timezone'] not in available:
            raise ValueError('Cannot validate current and requested timezones for reversible change.')
        plan['observations']['previous_timezone'] = previous
        if previous != values['Timezone']:
            plan['apply_commands'] = [['timedatectl', 'set-timezone', values['Timezone']]]
            plan['restore_commands'] = [['timedatectl', 'set-timezone', previous]]
    if module not in {'time', 'ssh'}:
        changed = any(change['before_sha256'] != hashlib.sha256(change['content'].encode('utf-8')).hexdigest() for change in plan['files'])
        target_active = snapshot['active'] if service_active is None else service_active
        if changed and snapshot['active'] and target_active:
            plan['apply_commands'].append(['systemctl', 'reload' if module == 'apache' else 'restart', snapshot['unit']])
    return plan


def _same(key, actual, expected):
    if key == 'TCPKeepAlive':
        # New OpenSSH names the historical yes behavior transport.
        return ('yes' if str(actual).lower() == 'transport' else str(actual).lower()) == expected
    if key in {'short_open_tag', 'session.upload_progress.enabled', 'expose_php', 'slow_query_log', 'log_queries_not_using_indexes'}:
        def boolean(value):
            value = str(value).strip().lower()
            if value in {'1', 'on', 'yes', 'true'}:
                return True
            if value in {'', '0', 'off', 'no', 'false'}:
                return False
            return None
        return boolean(actual) is not None and boolean(actual) == boolean(expected)
    if key == 'long_query_time':
        from decimal import Decimal, InvalidOperation
        try:
            return Decimal(str(actual)) == Decimal(expected)
        except InvalidOperation:
            return False
    if key in {'memory_limit', 'post_max_size', 'upload_max_filesize'}:
        def size(value):
            if not re.fullmatch(r'-1|[1-9][0-9]*[KMGkmg]?', str(value)):
                return None
            return int(value[:-1]) * 1024 ** ('KMG'.index(value[-1].upper()) + 1) if value[-1].upper() in 'KMG' else int(value)
        return size(actual) is not None and size(actual) == size(expected)
    return str(actual).strip().lower() == expected.strip().lower()


def _verify_php_evidence(host, target, scope, evidence, values):
    if evidence.get('sapi') != ('cli' if scope == 'cli' else 'fpm-fcgi'):
        raise PHPVerificationError(scope, 'sapi')
    if scope == 'cli' and evidence.get('loaded_ini') != target['main_ini']:
        raise PHPVerificationError(scope, 'loaded-ini', source=target['main_ini'])
    scanned = [item.strip() for item in evidence.get('scanned', '').replace('\n', ',').split(',') if item.strip()]
    if target['ini'] not in scanned:
        raise PHPVerificationError(scope, 'scanned-ini', source=target['ini'])
    if not all(evidence.get('extensions', {}).get(key) is True for key in ('pdo_sqlite', 'sqlite3')):
        raise PHPVerificationError(scope, 'extensions', source=target['ini'])
    effective = evidence.get('values', {})
    _, sources = _php_configuration(host, target['version'], scope)
    for key, expected in values.items():
        if key not in effective or not _same(key, effective[key], expected):
            source = sources.get(key, target['ini'])
            raise PHPVerificationError(scope, 'value', key=key, source=source)
    return {'verified': True, 'ini': target['ini'], 'values': {key: effective[key] for key in values}}


def verify_plan(host, plan):
    module, values = plan['module'], plan['values']
    result = {'verified': False, 'health_scope': _SCOPES[module], 'detail': ''}
    if module == 'time':
        observed = _properties(_run(host, ['timedatectl', 'show', '--property=Timezone', '--property=NTPSynchronized']))
        if observed.get('Timezone') != values['Timezone']:
            raise ValueError('Timezone runtime value differs from the selected target.')
        result.update(verified=True, detail=_SCOPES[module], ntp_synchronized=observed.get('NTPSynchronized') == 'yes')
        return result
    if module == 'ssh':
        for argv in plan['validate']:
            _run(host, argv)
        effective = verify_ssh_configuration(host, plan)
        service = _service(host, 'ssh.service')
        result.update(verified=True, detail=_SCOPES[module], global_values=effective,
                      service_active=service['active'], runtime_verified=False)
        return result
    snapshot = _service(host, plan['service_snapshot']['unit'])
    target_active = plan['service_snapshot']['active'] if plan.get('service_active') is None else plan['service_active']
    target_enabled = plan['service_snapshot']['enabled'] if plan.get('service_enabled') is None else plan['service_enabled']
    if snapshot['active'] != target_active:
        raise ValueError('Service active state differs from the selected target.')
    if snapshot['enabled'] != target_enabled:
        raise ValueError('Service boot enablement differs from the selected target.')
    for argv in plan['validate']:
        _run(host, argv)
    if module == 'php':
        from .root_fpm import probe_cli_values
        target = plan['observations']['fpm']
        cli = plan['observations']['cli']
        if _fpm(host) != target or _cli(host, target['version']) != cli:
            raise ValueError('PHP CLI/FPM target layout changed before independent verification.')
        result['sapis'] = {'cli': _verify_php_evidence(host, cli, 'cli',
            probe_cli_values(host, cli['binary']), values)}
    if not snapshot['active']:
        prefix = 'PHP CLI values verified; FPM settings saved, but ' if module == 'php' else 'Settings saved; '
        result['detail'] = prefix + 'service is inactive, so its runtime values and service health remain unverified. ' + _SCOPES[module]
        return result
    if module == 'php':
        from .root_fpm import probe_values
        result['sapis']['fpm'] = _verify_php_evidence(host, target, 'fpm',
            probe_values(host, target['socket']), values)
        result.update(verified=True, detail=_SCOPES[module])
        return result
    elif module == 'mariadb':
        query = "SHOW GLOBAL VARIABLES WHERE Variable_name IN ('slow_query_log','long_query_time','log_queries_not_using_indexes')"
        output = _run(host, ['mariadb', '--no-defaults', '--protocol=socket', '--connect-timeout=5',
                             '--batch', '--skip-column-names', '--execute', query], timeout=15)
        effective = dict(line.split('\t', 1) for line in output.splitlines() if '\t' in line)
    elif module == 'samba':
        effective = {key: _run(host, ['testparm', '--suppress-prompt', '--section-name=global',
                                    '--parameter-name=' + key, '/etc/samba/smb.conf']).strip() for key in values}
    elif module == 'vsftpd':
        effective = _ini_values(_read_required(host, '/etc/vsftpd.conf'), values)
    else:
        _apache_layout(host)
        if host.read(_APACHE_ENABLED) != _MARKER + '\nInclude ' + _APACHE_AVAILABLE + '\n':
            raise ValueError('Managed Apache configuration is not included.')
        included = _run(host, ['apache2ctl', '-t', '-D', 'DUMP_INCLUDES'])
        parsed = set(re.findall(r'^\s*\([^)]*\)\s+(/[^\r\n]+)\s*$', included, re.M))
        if not {_APACHE_AVAILABLE, _APACHE_ENABLED}.issubset(parsed):
            raise ValueError('Apache parser did not include both managed configuration files.')
        effective = dict(re.findall(r'^\s*(ServerTokens|ServerSignature)\s+(\S+)', host.read(_APACHE_AVAILABLE) or '', re.M))
    for key, expected in values.items():
        if key not in effective or not _same(key, effective[key], expected):
            raise ValueError(f'{key}: checked effective value differs from the selected target.')
    result.update(verified=True, detail=_SCOPES[module])
    return result


def post_install(host, module):
    result = {'integrated': False, 'detail': 'Package effects are not guaranteed reversible; no Apache/FPM integration was needed.'}
    if module not in {'apache', 'php'} or not _installed(host, 'apache'):
        return result
    try:
        target = _fpm(host)
        _apache_layout(host)
        if _service(host, target['unit'])['load_state'] != 'loaded':
            result['detail'] = 'FPM service unit is not installed; Apache integration was skipped.'
            return result
        enabled = host.path('/etc/apache2/mods-enabled')
        # Inspect names rather than dereference distro module symlinks.
        if enabled.is_dir() and any(file.name == 'mpm_prefork.load' or re.fullmatch(r'php[0-9.]*\.load', file.name) for file in enabled.iterdir()):
            result['detail'] = 'Existing prefork/mod_php needs a separate migration; no modules changed.'
            return result
        _read_required(host, f"/etc/apache2/conf-available/php{target['version']}-fpm.conf")
        _run(host, ['a2enmod', 'proxy_fcgi', 'setenvif'])
        _run(host, ['a2enconf', f"php{target['version']}-fpm"])
        _run(host, ['apache2ctl', 'configtest'])
        apache = _service(host, 'apache2.service')
        if apache['active']:
            _run(host, ['systemctl', 'reload', 'apache2.service'])
        result.update(integrated=True, detail='Enabled distribution proxy_fcgi/setenvif and matching FPM handler; HTTP application routing remains unverified. Package/module effects are not guaranteed reversible.')
    except (ValueError, OSError) as error:
        result['detail'] = 'Apache/FPM integration unavailable: ' + str(error)
    return result

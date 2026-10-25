"""Ubuntu MariaDB IPv4 network plans; the caller owns locking and transactions.

This module never changes accounts, files, services or firewall rules. Only
selected server variables and socket ownership are observed during verification.
"""
import hashlib
import ipaddress
import os
import re
import shlex
import stat

from .root_recipes import _mysql_layout, _properties, _run


ACCESS_PATH = '/etc/mysql/mariadb.conf.d/99-3wa-server-access.cnf'
MARKER = '# Managed by 3waServerSetup; MariaDB network access only.'
UNIT = 'mariadb.service'
_POSITION_FILE = '/run/mysqld/wsrep-start-position'
_POSITION = re.compile(r'--wsrep_start_position=[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}:-?[0-9]+')
_SCOPE = ('Selected MariaDB runtime bind/skip-networking/port and MariaDB-owned IPv4 LISTEN sockets only; '
          'firewall policy and access from an external client are not verified.')


def _enabled(value):
    if type(value) is not bool:
        raise ValueError('Network target must be an explicit boolean.')
    return value


def _native_file(host, absolute):
    path = host.path(absolute)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Native MariaDB unit must be a regular file.')
    for entry in (path, *path.parents):
        info = entry.stat()
        if os.name == 'posix' and (info.st_uid != 0 or info.st_mode & 0o022):
            raise ValueError('Native MariaDB unit and parents must be root controlled.')
        if entry == host.root:
            break


def _environment(text, *, position_file=False, manager=False):
    try:
        entries = shlex.split(text, comments=position_file)
    except ValueError:
        raise ValueError('Unsupported MariaDB environment format.') from None
    for entry in entries:
        if '=' not in entry:
            raise ValueError('Unsupported MariaDB environment format.')
        key, value = entry.split('=', 1)
        if manager and key not in {'MYSQLD_OPTS', 'MYSQL_HOME', 'MARIADB_HOME', 'MYSQLD_MULTI_INSTANCE',
                                   '_WSREP_NEW_CLUSTER', '_WSREP_START_POSITION'}:
            continue
        if key == '_WSREP_START_POSITION' and (not value or _POSITION.fullmatch(value)):
            continue
        if not position_file and key in {'MYSQLD_OPTS', '_WSREP_NEW_CLUSTER'} and not value:
            continue
        # Never put raw environment values or file contents into diagnostics.
        raise ValueError('Custom MariaDB environment/recovery options are unsupported.')


def _native(host):
    _mysql_layout(host)
    names = ('LoadState', 'ActiveState', 'UnitFileState', 'MainPID', 'User', 'FragmentPath',
             'DropInPaths', 'Environment', 'EnvironmentFiles', 'PassEnvironment',
             'RootDirectory', 'RootImage', 'TriggeredBy', 'ExecStart')
    observed = _properties(_run(host, ['systemctl', 'show', UNIT, '--all', '--no-pager',
                                      *['--property=' + key for key in names]]))
    # systemctl emits no rows for an empty EnvironmentFiles a(sb) array.
    observed.setdefault('EnvironmentFiles', '')
    if not set(names).issubset(observed):
        raise ValueError('Cannot determine the native MariaDB unit layout.')
    if (observed['LoadState'] != 'loaded' or observed['ActiveState'] != 'active'
            or observed['UnitFileState'] not in {'enabled', 'disabled'}):
        raise ValueError('An active native MariaDB service with reversible enablement state is required.')
    if any(observed[key] for key in ('DropInPaths', 'PassEnvironment', 'RootDirectory', 'RootImage', 'TriggeredBy')):
        raise ValueError('Custom MariaDB unit, filesystem or socket-activation layout is unsupported.')
    if observed['User'] != 'mysql' or observed['FragmentPath'] != '/usr/lib/systemd/system/mariadb.service':
        raise ValueError('Only the native Ubuntu MariaDB unit is supported.')
    _native_file(host, observed['FragmentPath'])
    executable = re.findall(r'(?:^|[;{\s])path=([^;\s]+)\s*;', observed['ExecStart'])
    argv = re.findall(r'argv\[\]=(.*?)\s*;', observed['ExecStart'])
    expected = ['/usr/sbin/mariadbd', '$MYSQLD_OPTS', '$_WSREP_NEW_CLUSTER', '$_WSREP_START_POSITION']
    if executable != [expected[0]] or len(argv) != 1 or argv[0].split() != expected:
        raise ValueError('Custom MariaDB ExecStart is unsupported.')
    _environment(observed['Environment'])
    if observed['EnvironmentFiles']:
        if observed['EnvironmentFiles'] != _POSITION_FILE + ' (ignore_errors=yes)':
            raise ValueError('Custom MariaDB environment files are unsupported.')
        content = host.read(_POSITION_FILE)
        if content is not None:
            _environment(content, position_file=True)
    _environment(_run(host, ['systemctl', 'show-environment']), manager=True)
    units = _run(host, ['systemctl', 'list-units', '--all', '--type=service', '--plain', '--no-legend',
                        '--no-pager', 'mariadb*.service', 'mysql*.service', 'mysqld*.service'])
    for line in units.splitlines():
        if line.strip() and line.split()[0] not in {UNIT, 'mysql.service', 'mysqld.service'}:
            raise ValueError('Multiple or unknown MariaDB service instances are unsupported.')
    pid = observed['MainPID']
    if not re.fullmatch(r'[1-9][0-9]*', pid):
        raise ValueError('Cannot determine the active MariaDB process.')
    processes = []
    for entry in host.path('/proc').iterdir():
        if not re.fullmatch(r'[1-9][0-9]*', entry.name):
            continue
        try:
            comm = host.read(f'/proc/{entry.name}/comm')
        except (OSError, UnicodeError):
            continue
        if comm and comm.strip() in {'mariadbd', 'mysqld'}:
            processes.append(entry.name)
    if processes != [pid]:
        raise ValueError('Multiple or unknown MariaDB server processes are unsupported.')
    return {'unit': UNIT, 'active': True, 'enabled': observed['UnitFileState'] == 'enabled',
            'load_state': 'loaded', 'unit_file_state': observed['UnitFileState'], 'main_pid': int(pid)}


def _variables(host):
    query = "SHOW GLOBAL VARIABLES WHERE Variable_name IN ('bind_address','skip_networking','port','wsrep_on')"
    text = _run(host, ['mariadb', '--no-defaults', '--protocol=socket', '--connect-timeout=5',
                       '--batch', '--skip-column-names', '--execute', query], timeout=15)
    values = dict(line.split('\t', 1) for line in text.splitlines() if '\t' in line)
    if set(values) != {'bind_address', 'skip_networking', 'port', 'wsrep_on'}:
        raise ValueError('Selected MariaDB network variables are unavailable.')
    if values['skip_networking'].upper() not in {'OFF', 'ON', '0', '1'}:
        raise ValueError('Unknown MariaDB skip_networking value.')
    if values['port'] != '3306':
        raise ValueError('Only the native MariaDB TCP port 3306 is supported.')
    if values['wsrep_on'].upper() not in {'OFF', '0'}:
        raise ValueError('Galera/wsrep cluster layouts are unsupported.')
    return {'bind_address': values['bind_address'], 'skip_networking': values['skip_networking'].upper() in {'ON', '1'}, 'port': 3306}


def _listeners(host, pid):
    # /proc/net is a symlink to self/net. The numeric equivalent keeps Host's
    # strict managed-path symlink rule intact, without following arbitrary links.
    text = host.read(f'/proc/{os.getpid()}/net/tcp')
    lines = text.splitlines() if text else []
    if not lines or 'local_address' not in lines[0]:
        raise ValueError('IPv4 LISTEN evidence is unavailable.')
    sockets = set()
    try:
        for entry in host.path(f'/proc/{pid}/fd').iterdir():
            if entry.name.isascii() and entry.name.isdecimal() and entry.is_symlink():
                match = re.fullmatch(r'socket:\[([0-9]+)\]', entry.readlink().as_posix())
                if match:
                    sockets.add(match[1])
    except OSError:
        raise ValueError('Cannot determine MariaDB process socket ownership.') from None
    owned, foreign = [], []
    for line in lines[1:]:
        fields = line.split()
        if len(fields) < 10:
            raise ValueError('Malformed native IPv4 socket table.')
        if fields[3] != '0A':
            continue
        match = re.fullmatch(r'([0-9A-Fa-f]{8}):([0-9A-Fa-f]{4})', fields[1])
        if not match:
            raise ValueError('Malformed IPv4 LISTEN address.')
        if int(match[2], 16) != 3306:
            continue
        address = str(ipaddress.IPv4Address(bytes.fromhex(match[1])[::-1])) + ':3306'
        (owned if fields[9] in sockets else foreign).append(address)
    if foreign:
        raise ValueError('A TCP 3306 LISTEN socket belongs to another process; network verification is refused.')
    return sorted(set(owned))


def network_status(host):
    snapshot = _native(host)
    values = _variables(host)
    listeners = _listeners(host, snapshot['main_pid'])
    remote = (True if listeners == ['0.0.0.0:3306'] and not values['skip_networking'] else
              False if listeners == ['127.0.0.1:3306'] or not listeners and values['skip_networking'] else None)
    return {**values, 'ipv4_listeners': listeners, 'remote_ipv4': remote,
            'service_snapshot': snapshot, 'health_scope': _SCOPE}


def _matches(observed, enabled):
    address = '0.0.0.0' if enabled else '127.0.0.1'
    return (observed['bind_address'] == address and not observed['skip_networking']
            and observed['port'] == 3306 and observed['ipv4_listeners'] == [address + ':3306'])


def network_plan(host, enabled):
    enabled = _enabled(enabled)
    observed = network_status(host)
    before = host.read(ACCESS_PATH)
    if before is not None and not before.startswith(MARKER + '\n'):
        raise ValueError('Existing MariaDB access file has unknown ownership/managed marker.')
    content = MARKER + '\n[mariadbd]\nbind-address = ' + ('0.0.0.0' if enabled else '127.0.0.1') + '\nskip-networking = 0\n'
    data = host.path(ACCESS_PATH).read_bytes() if before is not None else None
    changed = data != content.encode('utf-8')
    return {'schema': 1, 'enabled': enabled,
            'files': [{'path': ACCESS_PATH, 'before_sha256': hashlib.sha256(data).hexdigest() if data is not None else None,
                       'content': content, 'mode': 0o644}],
            'service_snapshot': observed['service_snapshot'],
            'validate': [['mariadbd', '--defaults-file=/etc/mysql/my.cnf', '--verbose', '--help']],
            'apply_commands': [['systemctl', 'restart', UNIT]] if changed or not _matches(observed, enabled) else [],
            'observations': observed, 'health_scope': _SCOPE}


def verify_network(host, enabled):
    enabled = _enabled(enabled)
    observed = network_status(host)
    if observed['skip_networking']:
        raise ValueError('MariaDB skip_networking differs from the selected target.')
    if not _matches(observed, enabled):
        raise ValueError('MariaDB runtime bind address or owned IPv4 LISTEN sockets differ from the selected target.')
    return {**observed, 'verified': True}

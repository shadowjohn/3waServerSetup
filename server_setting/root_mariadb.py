"""Reviewed MariaDB root access changes with RAM-only credential compensation.

Native authentication/locking semantics:
https://mariadb.com/docs/server/reference/sql-statements/account-management-sql-statements/alter-user
https://mariadb.com/docs/server/security/user-account-management/account-locking
"""
import hashlib
import hmac
import json
import re
import secrets

from .root_actions import BACKUPS, RootActionError, _digest, _fingerprint, _json

AUDIT = ("SELECT COUNT(*) FROM INFORMATION_SCHEMA.PLUGINS WHERE PLUGIN_STATUS='ACTIVE' "
         "AND (PLUGIN_TYPE='AUDIT' OR LOWER(PLUGIN_NAME) LIKE '%audit%');")
ACCOUNTS = ("SELECT Host, JSON_COMPACT(Priv) FROM mysql.global_priv "
            "WHERE User='root' AND Host IN ('localhost','%') ORDER BY Host;")
HASH = re.compile(r'\*[0-9A-Fa-f]{40}')
SCOPE = ('Local root Unix socket administration and selected root account state; IPv4 MariaDB listener. '
         'Firewall, TLS and a connection from another host are not verified.')


def _network():
    from . import mariadb_network
    return mariadb_network


def _sql(manager, statement):
    try:
        result = manager.host.mariadb_sql(statement, timeout=30)
        return result.stdout if hasattr(result, 'stdout') else result
    except RootActionError:
        raise
    except Exception:
        raise RootActionError('MariaDB SQL could not complete; native output was discarded.', 'MARIADB_SQL_FAILED') from None


def _snapshot(manager):
    if _sql(manager, AUDIT).strip() != '0':
        raise RootActionError('An active database audit plugin prevents credential changes.', 'MARIADB_AUDIT_ACTIVE')
    accounts = {}
    try:
        for row in _sql(manager, ACCOUNTS).splitlines():
            host, text = row.split('\t', 1)
            if host not in {'localhost', '%'} or host in accounts:
                raise ValueError()
            priv = json.loads(text)
            if not isinstance(priv, dict):
                raise ValueError()
            created = _sql(manager, f"SHOW CREATE USER 'root'@'{host}';").strip().split('\t', 1)[-1]
            grants = _sql(manager, f"SHOW GRANTS FOR 'root'@'{host}';").strip().splitlines()
            if not re.match(r"^CREATE USER ['`]root['`]@['`]" + re.escape(host) + r"['`]\s", created):
                raise ValueError()
            if not grants or any(not line.startswith('GRANT ') for line in grants):
                raise ValueError()
            accounts[host] = {'priv': priv, 'create': created.rstrip(';'), 'grants': [line.rstrip(';') for line in grants]}
    except (ValueError, TypeError, KeyError):
        raise RootActionError('Unsupported root account metadata.', 'MARIADB_UNSUPPORTED') from None
    local = accounts.get('localhost')
    if not local or local['priv'].get('account_locked', False):
        raise RootActionError('Unlocked root@localhost socket administration is required.', 'MARIADB_UNSUPPORTED')
    methods = _methods(local)
    if 'unix_socket' not in methods or set(methods) - {'unix_socket', 'mysql_native_password'}:
        raise RootActionError('The local root authentication layout is unsupported.', 'MARIADB_UNSUPPORTED')
    return {'accounts': accounts}


def _methods(account):
    priv = account['priv']
    primary = {'plugin': priv.get('plugin'), 'authentication_string': priv.get('authentication_string', '')}
    result = {}
    for item in [primary, *priv.get('auth_or', [])]:
        item = item or primary
        if isinstance(item, dict) and isinstance(item.get('plugin'), str):
            result[item['plugin']] = item.get('authentication_string', '')
    return result


def _native_only(account):
    return set(_methods(account)) == {'mysql_native_password'}


def _alter_auth(host, account, native=None, locked=None):
    methods = _methods(account)
    if native is not None:
        methods['mysql_native_password'] = native
    clauses = []
    for plugin, value in methods.items():
        if plugin not in {'mysql_native_password', 'unix_socket'}:
            raise RootActionError('Unsupported authentication policy.', 'MARIADB_UNSUPPORTED')
        if plugin == 'mysql_native_password' and value not in {'', 'invalid'} and not HASH.fullmatch(value):
            raise RootActionError('Unsupported authentication policy.', 'MARIADB_UNSUPPORTED')
        if plugin == 'unix_socket' and value not in {'', 'root'}:
            raise RootActionError('Unsupported socket authentication mapping.', 'MARIADB_UNSUPPORTED')
        clauses.append(plugin + (" USING '" + value + "'" if plugin == 'mysql_native_password' or value else ''))
    suffix = '' if locked is None else (' ACCOUNT LOCK' if locked else ' ACCOUNT UNLOCK')
    return f"ALTER USER 'root'@'{host}' IDENTIFIED VIA " + ' OR '.join(clauses) + suffix + ';'


def _native(account):
    value = _methods(account).get('mysql_native_password', '')
    return value.upper() if isinstance(value, str) and HASH.fullmatch(value) else None


def _full_root(account):
    return any(re.match(r"^GRANT ALL PRIVILEGES ON \*\.\* TO ['`]root['`]@['`]%['`]", grant)
               and 'WITH GRANT OPTION' in grant for grant in account['grants'])


def _public(snapshot, network):
    local = snapshot['accounts']['localhost']
    remote = snapshot['accounts'].get('%')
    locked = bool(remote['priv'].get('account_locked', False)) if remote else None
    enabled = bool(network['remote_ipv4'] and remote and not locked)
    return {'installed': True, 'active': True, 'supported': True,
            'detail': 'Live selected root authentication and listener flags; no password or authentication hash is returned.',
            'remote_enabled': enabled, 'root_remote_exists': remote is not None, 'root_remote_locked': locked,
            'bind_address': network['bind_address'], 'local_socket_preserved': True,
            'local_password_set': _native(local) is not None,
            'root_remote_password_set': (None if remote and not _native_only(remote) else bool(remote and _native(remote))),
            'root_remote_native_password_policy': bool(remote and _native_only(remote)),
            'root_remote_full_privileges': bool(remote and _full_root(remote)),
            'password_required_for_remote_on': _native(local) is None, 'health_scope': SCOPE}


def _require_baseline(network):
    address = network.get('bind_address')
    if (address not in {'127.0.0.1', '0.0.0.0'} or network.get('skip_networking') is not False
            or type(network.get('port')) is not int or network['port'] != 3306
            or type(network.get('remote_ipv4')) is not bool
            or network['remote_ipv4'] != (address == '0.0.0.0')
            or network.get('ipv4_listeners') != [address + ':3306']):
        raise RootActionError('The current IPv4 TCP layout cannot be safely restored by this version.', 'MARIADB_UNSUPPORTED')


def status(manager):
    network = None
    try:
        manager._gate('mariadb')
        network = _network().network_status(manager.host)
        _require_baseline(network)
        return _public(_snapshot(manager), network)
    except Exception as error:
        code = error.code if isinstance(error, RootActionError) else 'MARIADB_NOT_READY'
        return {'installed': network is not None, 'active': bool(network and network.get('service_snapshot', {}).get('active')),
                'supported': False, 'detail': code,
                'remote_enabled': None, 'root_remote_exists': None, 'root_remote_locked': None,
                'bind_address': network.get('bind_address') if network else None, 'local_socket_preserved': False, 'local_password_set': None,
                'password_required_for_remote_on': True, 'health_scope': SCOPE}


def _state_hmac(manager, snapshot, network):
    key = getattr(manager, '_mariadb_hmac_key', None)
    if key is None:
        key = manager._mariadb_hmac_key = secrets.token_bytes(32)
    return hmac.new(key, _json({'accounts': snapshot, 'network': network}).encode('utf-8'), hashlib.sha256).hexdigest()


def _build(manager, remote_enabled, change_password):
    manager._gate('mariadb')
    if remote_enabled is not None and type(remote_enabled) is not bool or type(change_password) is not bool:
        raise RootActionError('Remote and password targets must be booleans.', 'MARIADB_UNSUPPORTED')
    native = _network().network_status(manager.host)
    _require_baseline(native)
    snapshot = _snapshot(manager)
    observed = _public(snapshot, native)
    target = remote_enabled
    remote = snapshot['accounts'].get('%')
    if remote and not _native_only(remote) and (change_password or target is True):
        raise RootActionError('The existing remote authentication policy cannot be safely replaced.', 'MARIADB_UNSUPPORTED')
    network = (_network().network_plan(manager.host, target) if target is not None else
               {'schema': 1, 'enabled': None, 'files': [], 'service_snapshot': native['service_snapshot'],
                'validate': [], 'apply_commands': [], 'observations': native, 'health_scope': native['health_scope']})
    for entry in network['files']:
        entry['before_metadata_sha256'] = _fingerprint(manager._metadata(manager.host.path(entry['path'])) or {})
    result = {'schema': 1, 'module': 'mariadb-security', 'remote_enabled': target,
              'requested_remote_enabled': remote_enabled, 'change_password': change_password,
              'password_required': change_password or bool(target and not observed['local_password_set']),
              'observed': observed, 'network': network,
              'state_fingerprint': _state_hmac(manager, snapshot, network),
              'notes': ["The explicitly selected remote administrator is root@'%'; source restrictions belong to the firewall.",
                        'Local root@localhost Unix socket administration is preserved.',
                        'A new password updates local root native fallback and an existing root@%; explicit ON/OFF may create the locked remote account.',
                        'Keep leaves listener files, restarts, remote account existence, lock and grants unchanged.',
                        'ON prepares the locked account and grants before network restart; unlock is last. OFF locks first.',
                        'Credential compensation exists only in process memory; interruption requires manual recovery.',
                        'Configuration Restore last does not undo these credential or network-security changes.'],
              'health_scope': SCOPE}
    result['fingerprint'] = _fingerprint(result)
    return result, snapshot, native


def plan(manager, remote_enabled=None, change_password=False):
    return _build(manager, remote_enabled, change_password)[0]


def _password(password):
    if (not isinstance(password, str) or not password or any(char in password for char in '\x00\r\n')
            or len(password.encode('utf-8')) > 4096):
        raise RootActionError('A nonempty single-line password of at most 4096 UTF-8 bytes is required.', 'MARIADB_INVALID_PASSWORD')
    return '*' + hashlib.sha1(hashlib.sha1(password.encode('utf-8')).digest()).hexdigest().upper()


def _lock_remote(manager):
    _sql(manager, "ALTER USER 'root'@'%' ACCOUNT LOCK;")


def _restore_auth(manager, original, target, credentials_changed):
    current = _snapshot(manager)
    if target is not None and '%' in current['accounts']:
        _lock_remote(manager)
    if credentials_changed:
        _sql(manager, _alter_auth('localhost', original['accounts']['localhost']))
    old_remote = original['accounts'].get('%')
    if old_remote is None:
        if target is not None and '%' in current['accounts']:
            _sql(manager, "DROP USER IF EXISTS 'root'@'%';")
    elif credentials_changed or target is True:
        if '%' not in current['accounts']:
            raise RootActionError('Remote account disappeared during compensation.', 'RECOVERY_REQUIRED')
        _sql(manager, _alter_auth('%', old_remote, locked=True if target is not None else None))
        if target is True:
            _sql(manager, "REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'root'@'%';")
            for grant in old_remote['grants']:
                _sql(manager, grant + ';')
            _lock_remote(manager)


def _verify_auth_restored(manager, original):
    current = _snapshot(manager)
    if set(current['accounts']) != set(original['accounts']):
        raise RootActionError('Credential compensation could not be verified.', 'RECOVERY_REQUIRED')
    for host, before in original['accounts'].items():
        after = current['accounts'][host]
        if (_methods(before) != _methods(after)
                or bool(before['priv'].get('account_locked', False)) != bool(after['priv'].get('account_locked', False))
                or sorted(before['grants']) != sorted(after['grants'])):
            raise RootActionError('Credential compensation could not be verified.', 'RECOVERY_REQUIRED')


def apply(manager, preview, password=None):
    manager._gate('mariadb')
    if not isinstance(preview, dict) or preview.get('module') != 'mariadb-security' or preview.get('fingerprint') != _fingerprint(preview):
        raise RootActionError('Security preview was modified.', 'TAMPERED_PLAN')
    if password is not None and not preview.get('change_password'):
        raise RootActionError('Password replacement was not included in the review.', 'MARIADB_INVALID_PASSWORD')
    new_hash = _password(password) if password is not None else None
    if preview.get('password_required') and new_hash is None:
        raise RootActionError('A new password is required for this reviewed operation.', 'MARIADB_PASSWORD_REQUIRED')
    with manager._lock():
        manager._check_pending()
        trusted, original, native_before = _build(manager, preview.get('requested_remote_enabled'), preview.get('change_password'))
        if trusted['fingerprint'] != preview['fingerprint']:
            raise RootActionError('MariaDB account or listener state changed after preview.', 'STALE_PLAN')
        target = trusted['remote_enabled']
        network = trusted['network']
        selected_hash = new_hash or _native(original['accounts']['localhost'])
        if target is True and selected_hash is None:
            raise RootActionError('A new password is required before enabling remote root.', 'MARIADB_PASSWORD_REQUIRED')
        remote_before = original['accounts'].get('%')
        auth_change = bool(new_hash or target is True and (not remote_before or remote_before['priv'].get('account_locked')
                           or _native(remote_before) != selected_hash or not _full_root(remote_before))
                           or target is False and remote_before and not remote_before['priv'].get('account_locked', False))
        changes = []
        for entry in network['files']:
            path = manager.host.path(entry['path'])
            content = path.read_bytes() if path.exists() else None
            if _digest(content) != entry['before_sha256']:
                raise RootActionError('Network configuration changed after preview.', 'STALE_PLAN')
            if content != entry['content'].encode('utf-8'):
                changes.append(entry)
        if not auth_change and not changes and not network['apply_commands']:
            return {'status': 'NO_CHANGE', 'module': 'mariadb-security', 'message': 'Selected root access already matches.',
                    'verification': {'verified': True, 'health_scope': SCOPE, 'password_login_verified': False}}
        manager._preflight_state()
        identifier = manager._id()
        manager._secure_dir(f'{BACKUPS}/{identifier}')
        journal = {'schema': 1, 'transaction_id': identifier, 'module': 'mariadb-security', 'kind': 'mariadb-security',
                   'status': 'MARIADB_SECURITY_PREPARING', 'remote_enabled': target,
                   'password_changed': bool(new_hash), 'state_fingerprint': trusted['state_fingerprint'],
                   'files': [], 'phase': 'prepared',
                   'credential_recovery': 'Compensation is RAM-only; use local root Unix socket for manual recovery after interruption.'}
        metadata = {}
        for index, entry in enumerate(changes):
            path = manager.host.path(entry['path'])
            meta = manager._metadata(path)
            before = path.read_bytes() if meta else None
            name = f'{index:04d}.bin'
            if before is not None:
                manager._atomic(f'{BACKUPS}/{identifier}/{name}', before, {'mode': meta['mode'] & 0o600, 'uid': 0, 'gid': 0, 'xattrs': {}})
            metadata[entry['path']] = meta
            journal['files'].append({'path': entry['path'], 'backup': name, 'before_sha256': _digest(before),
                                     'after_sha256': _digest(entry['content'].encode('utf-8'))})
        manager._private_json(f'{BACKUPS}/{identifier}/metadata.json', metadata)
        manager._journal(journal)
        try:
            journal.update(status='MARIADB_SECURITY_APPLYING', phase='locking_remote')
            manager._journal(journal)
            if target is not None and remote_before:
                _lock_remote(manager)
            elif target is True or target is False and new_hash:
                _sql(manager, "CREATE USER 'root'@'%' IDENTIFIED VIA mysql_native_password USING 'invalid' ACCOUNT LOCK;")
            if new_hash:
                _sql(manager, _alter_auth('localhost', original['accounts']['localhost'], native=new_hash))
            if (target is True or new_hash and (remote_before or target is False)) and selected_hash:
                template = remote_before or {'priv': {'plugin': 'mysql_native_password', 'authentication_string': 'invalid'}}
                _sql(manager, _alter_auth('%', template, native=selected_hash, locked=True if target is not None else None))
            if target is True:
                _sql(manager, "GRANT ALL PRIVILEGES ON *.* TO 'root'@'%' WITH GRANT OPTION;")
            journal['phase'] = 'credentials_prepared_remote_locked'
            manager._journal(journal)
            for entry in changes:
                meta = metadata[entry['path']] or {'mode': entry.get('mode', 0o644), 'uid': 0, 'gid': 0, 'xattrs': {}}
                manager._atomic(entry['path'], entry['content'].encode('utf-8'), meta)
            for command in [*network['validate'], *network['apply_commands']]:
                manager._checked(command)
            _network().verify_network(manager.host, native_before['remote_ipv4'] if target is None else target)
            journal['phase'] = 'network_verified_remote_locked'
            manager._journal(journal)
            staged = _snapshot(manager)
            if new_hash and _native(staged['accounts']['localhost']) != new_hash:
                raise RootActionError('Local native password did not match the selected password.', 'MARIADB_SQL_FAILED')
            if new_hash and (remote_before or target is not None) and (
                    not staged['accounts'].get('%') or _native(staged['accounts']['%']) != new_hash):
                raise RootActionError('Remote native password did not match the selected password.', 'MARIADB_SQL_FAILED')
            if target is True and (not staged['accounts'].get('%') or _native(staged['accounts']['%']) != selected_hash
                           or not staged['accounts']['%']['priv'].get('account_locked') or not _full_root(staged['accounts']['%'])):
                raise RootActionError('Locked remote administrator preparation was not verified.', 'MARIADB_SQL_FAILED')
            if new_hash:
                identity = manager.host.mariadb_password_probe(password, port=native_before['port'], timeout=15).strip()
                if identity != 'root@localhost':
                    raise RootActionError('Loopback TCP matched an unexpected account.', 'MARIADB_PASSWORD_VERIFY_FAILED')
            if target is True:
                _sql(manager, "ALTER USER 'root'@'%' ACCOUNT UNLOCK;")
            final = _snapshot(manager)
            public = _public(final, _network().network_status(manager.host))
            if target is not None and public['remote_enabled'] != target or not public['local_socket_preserved']:
                raise RootActionError('Final root access did not match the selected target.', 'MARIADB_SQL_FAILED')
            if target is None:
                after_remote = final['accounts'].get('%')
                if bool(after_remote) != bool(remote_before) or (remote_before and
                        (bool(after_remote['priv'].get('account_locked', False)) != bool(remote_before['priv'].get('account_locked', False))
                         or after_remote['priv'].get('access') != remote_before['priv'].get('access')
                         or not new_hash and sorted(after_remote['grants']) != sorted(remote_before['grants']))):
                    raise RootActionError('Keep changed the existing remote account policy.', 'MARIADB_SQL_FAILED')
            journal.update(status='MARIADB_SECURITY_APPLIED', phase='verified')
            manager._journal(journal)
        except Exception:
            try:
                journal.update(status='MARIADB_SECURITY_ROLLING_BACK', phase='compensating')
                manager._journal(journal)
                _restore_auth(manager, original, target, bool(new_hash))
                manager._restore_files(journal)
                for command in network['validate']:
                    manager._checked(command)
                if network['apply_commands']:
                    manager._checked(['systemctl', 'restart', network['service_snapshot']['unit']])
                _network().verify_network(manager.host, native_before['remote_ipv4'])
                old_remote = original['accounts'].get('%')
                if target is not None and old_remote and not old_remote['priv'].get('account_locked', False):
                    _sql(manager, "ALTER USER 'root'@'%' ACCOUNT UNLOCK;")
                _verify_auth_restored(manager, original)
                journal.update(status='MARIADB_SECURITY_ROLLED_BACK', phase='compensation_verified')
                manager._journal(journal)
            except Exception:
                journal.update(status='RECOVERY_REQUIRED', phase='manual_socket_recovery_required')
                manager._journal(journal)
                raise RootActionError('MariaDB security compensation could not be verified; use local root Unix socket and inspect the private journal.', 'RECOVERY_REQUIRED') from None
            raise RootActionError('MariaDB security changes failed; original authentication and network configuration were restored.', 'MARIADB_ROLLED_BACK') from None
        return manager._log_result({'status': 'MARIADB_SECURITY_APPLIED', 'module': 'mariadb-security',
                    'transaction_id': identifier, 'message': 'Selected MariaDB root access applied; credentials are excluded from configuration Restore last.',
                    'verification': {'verified': True, 'health_scope': SCOPE + (' Password login was not tested; existing native authentication metadata was reused.' if not new_hash else ' Loopback TCP password authentication verified.'),
                                     'password_login_verified': bool(new_hash), 'local_socket_preserved': True,
                                     'remote_enabled': public['remote_enabled']}})

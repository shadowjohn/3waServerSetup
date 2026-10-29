"""Bounded root operations and reversible, filesystem-backed transactions.

Recipes choose every path and command. Caller-provided plans are only previews:
execution rebuilds them under the process lock and compares their fingerprint.
"""
from contextlib import contextmanager
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid

from .model import FIELDS, preview
from .operation_progress import phase_context, run_captured

try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None


TRANSACTIONS = '/var/lib/3wa-server-setup/transactions'
BACKUPS = '/opt/3waServerSetting/backups'
RECIPES = '/opt/3waServerSetting/config/recipes'
LOGS = '/var/lib/3wa-server-setup/logs'
_POSIX_PERMISSIONS = os.name == 'posix'
_RUNNING_CODE_ROOT = Path(__file__).resolve().parents[1]
TRUSTED_PATH = '/usr/sbin:/usr/bin:/sbin:/bin'
_LEGACY_FTP_FIELDS = frozenset({'anonymous_enable', 'local_enable', 'write_enable',
                                'pasv_min_port', 'pasv_max_port'})
COMMANDS = {'nft', 'iptables', 'ip6tables', 'iptables-save', 'ipset', 'dpkg-query', 'systemctl', 'sshd', 'apache2ctl', 'a2enconf', 'a2disconf',
            'a2enmod', 'testparm', 'mariadbd', 'mariadb', 'timedatectl', 'apt-get'}

# Recovery and ordinary operations share one terminal-state registry. A new
# module must not leave completed work blocking another module's recovery.
FINAL_STATUSES = frozenset({'APPLIED', 'RESTORED', 'ROLLED_BACK', 'INSTALLED', 'INSTALL_FAILED',
    'APT_UPDATED', 'APT_UPGRADED', 'APT_FAILED', 'MARIADB_SECURITY_APPLIED', 'MARIADB_SECURITY_ROLLED_BACK',
    'FW_CONFIRMED', 'FW_ROLLED_BACK', 'FW_BOOT_ROLLED_BACK', 'FW_RESTORED_INITIAL', 'FW_RESTORED_PREVIOUS',
    'LVM_APPLIED', 'LVM_FAILED', 'SWAP_APPLIED', 'SWAP_ROLLED_BACK',
    'BANIP_CONFIRMED', 'BANIP_ROLLED_BACK', 'BANIP_FAILED', 'PACKAGES_INSTALLED', 'PACKAGES_FAILED',
    'FEATHER_APPLIED', 'FEATHER_ROLLED_BACK', 'FEATHER_FAILED',
    'STARTUP_APPLIED', 'STARTUP_RESTORED', 'STARTUP_ROLLED_BACK',
    'STARTUP_RUN_SUCCEEDED', 'STARTUP_RUN_FAILED',
    'APACHE_FPM_APPLIED', 'APACHE_FPM_RESTORED', 'APACHE_FPM_ROLLED_BACK'})


class RootActionError(RuntimeError):
    """A deliberately sanitized error, suitable for a terminal or journal."""
    status = 'FAILED'

    def __init__(self, message, code='FAILED'):
        super().__init__(message)
        self.code = code


def _digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _json(data):
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _fingerprint(plan):
    return _digest(_json({k: v for k, v in plan.items() if k != 'fingerprint'}).encode('utf-8'))


class Host:
    """Native host boundary; a prefix permits real temporary filesystem tests."""

    def __init__(self, root=None):
        self.root = Path(root) if root is not None else Path('/').absolute()
        if not self.root.is_absolute():
            raise RootActionError('Host filesystem prefix must be absolute.')

    def path(self, absolute_path):
        if (not isinstance(absolute_path, str) or not absolute_path.startswith('/')
                or absolute_path.startswith('//') or '\\' in absolute_path
                or '\x00' in absolute_path or ':' in absolute_path
                or any(part in {'.', '..'} for part in absolute_path.split('/'))):
            raise RootActionError('Unsafe filesystem path.', 'UNSAFE_PATH')
        current = self.root
        for part in ['', *absolute_path.split('/')[1:]]:
            current = current / part
            if current.is_symlink():
                raise RootActionError('Symbolic links are not accepted for managed paths.', 'UNSAFE_PATH')
        return current

    def read(self, absolute_path):
        path = self.path(absolute_path)
        try:
            if not stat.S_ISREG(path.stat().st_mode):
                raise RootActionError('Managed input is not a regular file.', 'UNSAFE_PATH')
            return path.read_text(encoding='utf-8')
        except FileNotFoundError:
            return None

    def run(self, argv, timeout=30):
        if (not isinstance(argv, (tuple, list)) or not argv
                or any(not isinstance(arg, str) or '\x00' in arg for arg in argv)):
            raise RootActionError('Commands must be an argv list.', 'UNSAFE_COMMAND')
        name = Path(argv[0]).name
        if name not in COMMANDS and not re.fullmatch(r'php(?:-fpm)?[0-9]+\.[0-9]+', name):
            raise RootActionError('Command is outside the trusted allowlist.', 'UNSAFE_COMMAND')
        if '/' in argv[0] or '\\' in argv[0]:
            if argv[0] not in [directory + '/' + name for directory in TRUSTED_PATH.split(':')]:
                raise RootActionError('Executable path is outside trusted system directories.', 'UNSAFE_COMMAND')
        if self.root != Path('/'):
            raise RootActionError('Prefixed hosts require an explicit command test boundary.')
        executable = shutil.which(name, path=TRUSTED_PATH)
        if executable is None:
            raise RootActionError(f'Required system command is unavailable: {name}.', 'MISSING_COMMAND')
        resolved = Path(executable).resolve()
        for candidate in [resolved, *resolved.parents]:
            info = candidate.stat()
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise RootActionError('System executable or parent is not root controlled.', 'UNSAFE_COMMAND')
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 900:
            raise RootActionError('Command timeout must be bounded.')
        env = {'PATH': TRUSTED_PATH, 'LC_ALL': 'C', 'LANG': 'C',
               'DEBIAN_FRONTEND': 'noninteractive', 'NEEDRESTART_MODE': 'l', 'HOME': '/root'}
        try:
            result = run_captured([executable, *argv[1:]], timeout=timeout, command=name, env=env)
            if result.output_truncated and (name != 'apt-get' or '--simulate' in argv):
                raise RootActionError('Native command metadata exceeded the bounded capture.', 'OUTPUT_LIMIT')
            return result
        except subprocess.TimeoutExpired:
            raise RootActionError(f'{name}: timed out.', 'TIMEOUT') from None
        except OSError:
            raise RootActionError(f'{name}: could not execute.', 'COMMAND_FAILED') from None

    def _mariadb_client(self):
        if self.root != Path('/').absolute() or getattr(os, 'geteuid', lambda: -1)() != 0:
            raise RootActionError('Native root SQL requires the local privileged host.', 'MARIADB_NOT_READY')
        from .trusted_commands import resolve
        executable = resolve('mariadb')
        if not executable:
            raise RootActionError('A trusted MariaDB client is unavailable.', 'MARIADB_NOT_READY')
        return executable

    def _mariadb_environment(self):
        return {'PATH': TRUSTED_PATH, 'LC_ALL': 'C', 'LANG': 'C', 'HOME': '/root',
                'MYSQL_HISTFILE': '/dev/null'}

    def mariadb_sql(self, sql, timeout=30):
        """No secret SQL enters the client until its session logging guard responds."""
        if not isinstance(sql, str) or not 0 < timeout <= 60:
            raise RootActionError('Invalid bounded SQL request.', 'MARIADB_SQL_FAILED')
        argv = [self._mariadb_client(), '--no-defaults', '--protocol=socket', '--user=root',
                '--batch', '--skip-column-names', '--raw', '--unbuffered', '--skip-reconnect', '--binary-mode', '--sandbox',
                '--default-character-set=utf8mb4', '--connect-timeout=5']
        guards = ("SET SESSION sql_log_off=1;\nSET SESSION sql_log_bin=0;\nSET SESSION log_slow_query=0;\n"
                  "SELECT CASE WHEN EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.PLUGINS "
                  "WHERE PLUGIN_STATUS='ACTIVE' AND (PLUGIN_TYPE='AUDIT' OR LOWER(PLUGIN_NAME) LIKE '%audit%')) "
                  "THEN '__3WA_SQL_AUDIT__' WHEN @@session.sql_log_off=1 AND @@session.sql_log_bin=0 "
                  "AND @@session.log_slow_query=0 THEN '__3WA_SQL_READY__' ELSE '__3WA_SQL_BLOCKED__' END;\n")
        child = None
        reader = None
        completed = False
        try:
            deadline = time.monotonic() + timeout
            child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding='utf-8', errors='strict', shell=False,
                                     env=self._mariadb_environment())
            ready = threading.Event()
            acknowledgment = []
            def read_guard():
                try:
                    acknowledgment.append(child.stdout.readline().strip())
                except Exception:
                    acknowledgment.append('')
                finally:
                    ready.set()
            reader = threading.Thread(target=read_guard, daemon=True)
            reader.start()
            child.stdin.write(guards)
            child.stdin.flush()
            if not ready.wait(timeout):
                raise RootActionError('MariaDB session guard timed out.', 'MARIADB_SQL_FAILED')
            if acknowledgment != ['__3WA_SQL_READY__']:
                code = 'MARIADB_AUDIT_ACTIVE' if acknowledgment == ['__3WA_SQL_AUDIT__'] else 'MARIADB_SQL_FAILED'
                raise RootActionError('MariaDB session logging guard refused this operation.', code)
            stdout, _ = child.communicate(input=sql + '\n', timeout=max(.1, deadline - time.monotonic()))
            completed = True
            if child.returncode:
                raise RootActionError('MariaDB SQL failed; native diagnostic output was discarded.', 'MARIADB_SQL_FAILED')
            return stdout
        except RootActionError:
            raise
        except Exception:
            raise RootActionError('MariaDB SQL failed; native diagnostic output was discarded.', 'MARIADB_SQL_FAILED') from None
        finally:
            if child is not None:
                try:
                    if not completed:
                        child.kill()
                        child.communicate(timeout=2)
                except Exception:
                    pass
            if reader is not None:
                reader.join(timeout=1)

    def mariadb_password_probe(self, password, port=3306, timeout=15):
        """A mode0600 anonymous memfd carries only client options; protocol is TCP."""
        if (not isinstance(password, str) or not password or any(c in password for c in '\x00\r\n')
                or len(password.encode('utf-8')) > 4096 or type(port) is not int or not 1 <= port <= 65535):
            raise RootActionError('Invalid password probe request.', 'MARIADB_INVALID_PASSWORD')
        if not hasattr(os, 'memfd_create'):
            raise RootActionError('A memory-only option file is unavailable.', 'MARIADB_UNSUPPORTED')
        executable = self._mariadb_client()
        descriptor = os.memfd_create('3wa-mariadb-auth', flags=getattr(os, 'MFD_CLOEXEC', 0))
        try:
            os.fchmod(descriptor, 0o600)
            escaped = password.replace('\\', '\\\\').replace('"', '\\"').replace('\t', '\\t')
            data = ('[client]\npassword="' + escaped + '"\n').encode('utf-8')
            offset = 0
            while offset < len(data):
                offset += os.write(descriptor, data[offset:])
            os.lseek(descriptor, 0, os.SEEK_SET)
            argv = [executable, f'--defaults-file=/proc/self/fd/{descriptor}', '--protocol=tcp', '--host=127.0.0.1',
                    '--user=root', f'--port={port}', '--batch', '--skip-column-names', '--raw', '--connect-timeout=5', '--skip-reconnect', '--sandbox',
                    '--default-character-set=utf8mb4']
            result = subprocess.run(argv, input='SELECT CURRENT_USER();\n', capture_output=True, text=True,
                                    encoding='utf-8', errors='strict', timeout=timeout, check=False, shell=False,
                                    env=self._mariadb_environment(), pass_fds=(descriptor,))
            if result.returncode:
                raise RootActionError('Strict loopback TCP password authentication failed.', 'MARIADB_PASSWORD_VERIFY_FAILED')
            return result.stdout
        except RootActionError:
            raise
        except Exception:
            raise RootActionError('Strict loopback TCP password authentication failed.', 'MARIADB_PASSWORD_VERIFY_FAILED') from None
        finally:
            os.close(descriptor)


class RootManager:
    def __init__(self, host=None):
        self.host = host or Host()

    def _recipes(self):
        from . import root_recipes
        return root_recipes

    def _module(self, module):
        if module not in FIELDS or not FIELDS[module]:
            raise RootActionError('This page does not support root changes.', 'UNSUPPORTED')

    def _platform(self):
        self.host.path('/etc')
        native = self.host.root / 'etc/os-release'
        source = '/etc/os-release'
        if native.is_symlink():
            # Ubuntu's documented native alias is a read-only exception at this
            # boundary; managed files still never follow symbolic links.
            if native.readlink().as_posix() not in {'/usr/lib/os-release', '../usr/lib/os-release'}:
                raise RootActionError('Unsupported os-release symbolic link.', 'UNSAFE_PATH')
            source = '/usr/lib/os-release'
        values = {}
        for line in (self.host.read(source) or '').splitlines():
            if '=' in line:
                key, value = line.split('=', 1)
                values[key] = value.strip().strip('"').strip("'")
        return values.get('ID') == 'ubuntu' and values.get('VERSION_ID') == '26.04'

    def _gate(self, module):
        self._module(module)
        if getattr(os, 'geteuid', lambda: -1)() != 0:
            raise RootActionError('Root privileges are required.', 'ROOT_REQUIRED')
        if not self._platform():
            raise RootActionError('Writes are supported only on verified Ubuntu 26.04.', 'UNSUPPORTED_OS')
        self._release_gate()

    @staticmethod
    def _release_gate():
        from .release_update import ensure_current_release, ReleaseError
        try:
            ensure_current_release(_RUNNING_CODE_ROOT)
        except ReleaseError as error:
            raise RootActionError(str(error), error.code) from None

    def capabilities(self, module):
        self._module(module)
        result = dict(self._recipes().capabilities(self.host, module))
        platform = self._platform()
        root = getattr(os, 'geteuid', lambda: -1)() == 0
        result['supported'] = bool(result.get('supported', True) and platform and root)
        if not platform:
            result['detail'] = 'Writes are supported only on verified Ubuntu 26.04.'
        elif not root:
            result['detail'] = 'Root privileges are required for changes.'
        return result

    def recommended(self, module):
        self._module(module)
        return dict(FIELDS[module])

    def _values(self, module, values):
        self._module(module)
        values = self.recommended(module) if values is None else values
        if module == 'vsftpd' and isinstance(values, dict) and set(values) == _LEGACY_FTP_FIELDS:
            # 舊配方只補新欄位；任意其他不完整形狀仍走嚴格檢查。
            values = {**FIELDS[module], **values}
        if not isinstance(values, dict) or set(values) != set(FIELDS[module]):
            raise RootActionError('A complete, known set of recipe fields is required.', 'INVALID_VALUES')
        try:
            preview(module, values, {})
        except (ValueError, TypeError, OverflowError):
            raise RootActionError('One or more recipe values are invalid.', 'INVALID_VALUES') from None
        return dict(values)

    def load_saved(self, module):
        return self._load_saved(module, normalize=True)

    def _load_saved(self, module, normalize):
        self._module(module)
        path = self.host.path(f'{RECIPES}/{module}.json')
        if not path.exists():
            return None
        self._trusted_file(path)
        try:
            saved = json.loads(path.read_text(encoding='utf-8'))
            if saved['schema'] != 1 or saved['module'] != module:
                raise ValueError()
            normalized = self._values(module, saved['values'])
            for key in ('service_active', 'service_enabled'):
                if saved.get(key) is not None and type(saved[key]) is not bool:
                    raise ValueError()
            if set(saved) != {'schema', 'module', 'values', 'service_active', 'service_enabled'}:
                raise ValueError()
            return {**saved, 'values': normalized} if normalize else saved
        except (ValueError, KeyError, TypeError):
            raise RootActionError('Saved recipe is invalid; preview a new recipe.', 'INVALID_RECIPE') from None

    def plan(self, module, values=None, service_active=None, service_enabled=None):
        values = self._values(module, values)
        for target in (service_active, service_enabled):
            if target is not None and type(target) is not bool:
                raise RootActionError('Service targets must be true, false or unspecified.')
        plan = self._recipes().build_plan(self.host, module, values, service_active, service_enabled)
        if (plan.get('module') != module or plan.get('values') != values
                or plan.get('service_active') != service_active or plan.get('service_enabled') != service_enabled):
            raise RootActionError('Recipe produced an inconsistent plan.')
        snapshot = plan.get('service_snapshot', {})
        if snapshot.get('unit') and snapshot.get('unit_file_state', 'disabled') not in {'enabled', 'disabled'}:
            raise RootActionError('This service enablement state cannot be safely restored by this version.', 'UNSUPPORTED_STATE')
        seen = set()
        for entry in plan.get('files', []):
            path = self.host.path(entry['path'])
            if entry['path'] in seen or not isinstance(entry['content'], str):
                raise RootActionError('Recipe contains an invalid file target.')
            seen.add(entry['path'])
            entry['before_metadata_sha256'] = _fingerprint(self._metadata(path) or {})
        plan['fingerprint'] = _fingerprint(plan)
        return plan

    def install_plan(self, module):
        self._module(module)
        packages = self._recipes().packages(module)
        if any(not re.fullmatch(r'[a-z0-9][a-z0-9+.-]*', package) for package in packages):
            raise RootActionError('Recipe contains an invalid package name.')
        result = {'schema': 1, 'module': module, 'packages': list(packages),
                  'capabilities': self.capabilities(module),
                  'notes': ['Installs only selected packages. Package installation cannot be rolled back by this tool.']}
        result['fingerprint'] = _fingerprint(result)
        return result

    def install(self, module):
        self._gate(module)
        with self._lock():
            self._check_pending()
            plan = self.install_plan(module)
            if not plan['capabilities'].get('supported'):
                raise RootActionError('This host layout does not support the selected package action.')
            if not plan['packages']:
                return {'status': 'NO_CHANGE', 'module': module, 'message': 'No package installation is required.'}
            self._preflight_state()
            identifier = self._id()
            journal = {'schema': 1, 'transaction_id': identifier, 'module': module,
                       'kind': 'install', 'status': 'INSTALLING', 'packages': plan['packages']}
            self._journal(journal)
            try:
                with phase_context('package-install'):
                    self._checked(['apt-get', 'install', '--yes', '--no-install-recommends', *plan['packages']], timeout=900)
                integration = self._recipes().post_install(self.host, module)
            except Exception:
                journal['status'] = 'INSTALL_FAILED'
                self._journal(journal)
                self._log_result({'status': 'INSTALL_FAILED', 'module': module, 'transaction_id': identifier,
                                  'message': 'Package installation or integration failed.'})
                raise RootActionError('Package installation or integration failed; installed packages were not removed.', 'INSTALL_FAILED') from None
            journal['status'] = 'INSTALLED'
            self._journal(journal)
            return self._log_result({'status': 'INSTALLED', 'module': module, 'transaction_id': identifier,
                    'message': 'Selected packages installed; package removal is not part of configuration restore.',
                    'verification': integration})

    @contextmanager
    def _lock(self):
        if _fcntl is None:
            raise RootActionError('A native flock implementation is required; changes are disabled.', 'LOCK_UNAVAILABLE')
        directory = self._secure_dir('/run/3wa-server-setup')
        target = self.host.path('/run/3wa-server-setup/transaction.lock')
        descriptor = os.open(target, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            self._trusted_file(target)
            try:
                _fcntl.flock(descriptor, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            except OSError:
                raise RootActionError('Another root transaction is in progress.', 'BUSY') from None
            # The updater may have switched versions after the earlier gate.
            self._release_gate()
            yield
        finally:
            os.close(descriptor)

    def _secure_dir(self, absolute):
        path = self.host.path(absolute)
        missing = []
        current = path
        while not current.exists():
            missing.append(current)
            current = current.parent
        # Check the whole existing chain, not merely the leaf directory.
        for ancestor in [current, *current.parents]:
            if ancestor == self.host.root.parent and self.host.root != Path('/'):
                break
            info = ancestor.stat()
            if not stat.S_ISDIR(info.st_mode):
                raise RootActionError('State directory parent is not a directory.')
            if os.name == 'posix' and (info.st_uid != 0 or info.st_mode & 0o022):
                raise RootActionError('State directory is not root controlled.', 'UNSAFE_STATE')
            if ancestor == self.host.root:
                break
        for directory in reversed(missing):
            directory.mkdir(mode=0o700)
        if os.name == 'posix':
            os.chmod(path, 0o700)
        return path

    def _trusted_file(self, path):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise RootActionError('State input is not a regular file.', 'UNSAFE_STATE')
        if os.name == 'posix' and (info.st_uid != 0 or info.st_mode & 0o077):
            raise RootActionError('State input is not private to root.', 'UNSAFE_STATE')

    def _metadata(self, path):
        self._trusted_target(path)
        if not path.exists():
            return None
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise RootActionError('Managed target is not a regular file.', 'UNSAFE_PATH')
        attrs = {}
        if hasattr(os, 'listxattr'):
            try:
                attrs = {name: base64.b64encode(os.getxattr(path, name)).decode('ascii') for name in os.listxattr(path)}
            except OSError:
                raise RootActionError('Cannot safely preserve file extended attributes.') from None
        return {'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid, 'xattrs': attrs}

    def _trusted_target(self, path):
        if not _POSIX_PERMISSIONS:
            return
        for current in [path, *path.parents]:
            if current.exists():
                info = current.lstat()
                valid_kind = stat.S_ISREG(info.st_mode) if current == path else stat.S_ISDIR(info.st_mode)
                if not valid_kind or info.st_uid != 0 or info.st_mode & 0o022:
                    raise RootActionError('Managed target or parent is not root controlled.', 'UNSAFE_PATH')
            if current == self.host.root:
                break

    def _atomic(self, absolute, data, metadata):
        target = self.host.path(absolute)
        self._metadata(target)
        if not target.parent.is_dir():
            raise RootActionError('Managed file parent is missing; install the selected service first.', 'MISSING_PARENT')
        descriptor, temporary = tempfile.mkstemp(prefix='.3wa-', dir=target.parent)
        temporary = Path(temporary)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                if os.name == 'posix':
                    os.fchown(stream.fileno(), metadata['uid'], metadata['gid'])
                    os.fchmod(stream.fileno(), metadata['mode'])
                else:
                    os.chmod(temporary, metadata['mode'])
                for name, value in metadata.get('xattrs', {}).items():
                    os.setxattr(temporary, name, base64.b64decode(value))
            self.host.path(absolute)
            os.replace(temporary, target)
            if os.name == 'posix':
                directory_fd = os.open(target.parent, os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _private_json(self, absolute, value):
        self._atomic(absolute, (_json(value) + '\n').encode('utf-8'), {'mode': 0o600, 'uid': 0, 'gid': 0, 'xattrs': {}})

    def _id(self):
        return time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex[:12]

    def _journal(self, journal):
        journal.setdefault('created_ns', time.time_ns())
        self._secure_dir(f"{TRANSACTIONS}/{journal['transaction_id']}")
        self._private_json(f"{TRANSACTIONS}/{journal['transaction_id']}/journal.json", journal)

    def _log(self, module, status, identifier):
        self._secure_dir(LOGS)
        self._private_json(f'{LOGS}/{identifier}.json', {'module': module, 'status': status, 'transaction_id': identifier})

    def _preflight_state(self):
        for directory in (TRANSACTIONS, BACKUPS, RECIPES, LOGS):
            self._secure_dir(directory)

    def _log_result(self, result):
        try:
            self._log(result['module'], result['status'], result['transaction_id'])
        except Exception:
            result['status'] += '_WITH_LOG_WARNING'
            result['message'] += ' The transaction journal is saved, but the separate operation log could not be written.'
            result['warning'] = 'Operation log unavailable; inspect the authoritative transaction journal.'
        return result

    def _journals(self):
        directory = self.host.path(TRANSACTIONS)
        if not directory.exists():
            return []
        journals = []
        for child in sorted(directory.iterdir()):
            if not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}', child.name):
                raise RootActionError('Unrecognized transaction state; inspect the transaction directory.', 'UNSAFE_STATE')
            path = self.host.path(f'{TRANSACTIONS}/{child.name}/journal.json')
            self._trusted_file(path)
            try:
                journal = json.loads(path.read_text(encoding='utf-8'))
                if journal['transaction_id'] != child.name:
                    raise ValueError()
                journals.append(journal)
            except (ValueError, KeyError, TypeError):
                raise RootActionError('Invalid transaction journal; inspect the transaction directory.', 'UNSAFE_STATE') from None
        return sorted(journals, key=lambda journal: journal.get('created_ns', 0))

    def _check_pending(self, ignore_id=None):
        if any(journal.get('status') not in FINAL_STATUSES and journal['transaction_id'] != ignore_id
               for journal in self._journals()):
            raise RootActionError(f'An incomplete transaction requires inspection at {TRANSACTIONS}.', 'RECOVERY_REQUIRED')

    def _checked(self, argv, timeout=30):
        result = self.host.run(argv, timeout=timeout)
        if result.returncode:
            raise RootActionError(f'{Path(argv[0]).name}: checked command failed (exit {result.returncode}).', 'COMMAND_FAILED')
        return result

    def _service_restore(self, snapshot):
        unit = snapshot.get('unit')
        if unit is None:
            return
        if not re.fullmatch(r'[a-zA-Z0-9@_.-]+\.service', unit):
            raise RootActionError('Invalid saved service identity.')
        self._checked(['systemctl', 'enable' if snapshot.get('enabled') else 'disable', unit])
        self._checked(['systemctl', 'start' if snapshot.get('active') else 'stop', unit])
        if snapshot.get('active'):
            self._checked(['systemctl', 'reload-or-restart', unit])

    def _save_recipe(self, plan):
        self._secure_dir(RECIPES)
        saved = {key: plan[key] for key in ('schema', 'module', 'values', 'service_active', 'service_enabled')}
        self._private_json(f"{RECIPES}/{plan['module']}.json", saved)

    def _restore_recipe(self, journal):
        previous = journal.get('previous_recipe')
        if previous is not None:
            self._values(journal['module'], previous['values'])
            self._save_recipe(previous)
        else:
            path = self.host.path(f"{RECIPES}/{journal['module']}.json")
            if path.exists():
                self._trusted_file(path)
                path.unlink()

    def apply(self, plan):
        if not isinstance(plan, dict):
            raise RootActionError('An immutable preview plan is required.')
        module = plan.get('module')
        self._gate(module)
        with self._lock():
            self._check_pending()
            if plan.get('fingerprint') != _fingerprint(plan):
                raise RootActionError('Preview plan was modified; create a new preview.', 'TAMPERED_PLAN')
            trusted = self.plan(module, plan.get('values'), plan.get('service_active'), plan.get('service_enabled'))
            if trusted['fingerprint'] != plan['fingerprint']:
                raise RootActionError('Host state changed after preview; create a new preview.', 'STALE_PLAN')
            changed = []
            for entry in trusted['files']:
                path = self.host.path(entry['path'])
                current = path.read_bytes() if path.exists() else None
                if _digest(current) != entry['before_sha256']:
                    raise RootActionError('Managed file changed after preview.', 'STALE_PLAN')
                if current != entry['content'].encode('utf-8'):
                    changed.append(entry)
            snapshot = trusted.get('service_snapshot', {})
            service_change = any(trusted.get(key) is not None and trusted[key] != snapshot.get(field)
                                 for key, field in [('service_active', 'active'), ('service_enabled', 'enabled')])
            # Non-file resources (currently timezone) explicitly declare commands only when changed.
            resource_change = not trusted['files'] and bool(trusted.get('apply_commands'))
            # 顯示可補欄，回復備份則保留升級前原始配方的欄位形狀。
            previous_recipe = self._load_saved(module, normalize=False)
            saved_target = {key: trusted[key] for key in ('schema', 'module', 'values', 'service_active', 'service_enabled')}
            recipe_change = previous_recipe != saved_target
            if not changed and not service_change and not resource_change and not recipe_change:
                verification = self._recipes().verify_plan(self.host, trusted)
                return {'status': 'NO_CHANGE', 'module': module, 'message': 'Configuration already matches; no service reload was required.', 'verification': verification}
            self._preflight_state()
            identifier = self._id()
            self._secure_dir(f'{BACKUPS}/{identifier}')
            journal = {'schema': 1, 'transaction_id': identifier, 'module': module, 'kind': 'configuration',
                       'status': 'PREPARING', 'fingerprint': trusted['fingerprint'], 'values': trusted['values'],
                       'service_active': trusted.get('service_active'), 'service_enabled': trusted.get('service_enabled'),
                       'service_changed': service_change,
                       'service_snapshot': snapshot, 'restore_commands': trusted.get('restore_commands', []),
                       'observations': ({'previous_timezone': trusted.get('observations', {}).get('previous_timezone')} if module == 'time' else {}),
                       'previous_recipe': previous_recipe,
                       'validate': trusted.get('validate', []), 'files': []}
            manifest = {}
            # Every backup exists before the first live configuration write.
            for index, entry in enumerate(changed):
                path = self.host.path(entry['path'])
                metadata = self._metadata(path)
                original = path.read_bytes() if metadata else None
                backup = f'{index:04d}.bin'
                if original is not None:
                    self._atomic(f'{BACKUPS}/{identifier}/{backup}', original,
                                 {'mode': metadata['mode'] & 0o600, 'uid': 0, 'gid': 0, 'xattrs': {}})
                manifest[entry['path']] = metadata
                journal['files'].append({'path': entry['path'], 'before_sha256': _digest(original),
                                         'after_sha256': _digest(entry['content'].encode('utf-8')), 'backup': backup})
            self._private_json(f'{BACKUPS}/{identifier}/metadata.json', manifest)
            self._journal(journal)
            try:
                journal['status'] = 'APPLYING'
                self._journal(journal)
                for entry in changed:
                    metadata = manifest[entry['path']] or {'mode': entry.get('mode', 0o644), 'uid': 0, 'gid': 0, 'xattrs': {}}
                    self._atomic(entry['path'], entry['content'].encode('utf-8'), metadata)
                for command in trusted.get('validate', []):
                    self._checked(command)
                if module == 'ssh':
                    self._recipes().verify_ssh_configuration(self.host, trusted)
                for command in trusted.get('apply_commands', []):
                    # Reload commands are needed only when content actually changed.
                    if changed or resource_change or not any(word in {'reload', 'restart', 'reload-or-restart'} for word in command):
                        self._checked(command)
                unit = snapshot.get('unit')
                if unit:
                    for key, yes, no in [('service_enabled', 'enable', 'disable'), ('service_active', 'start', 'stop')]:
                        if trusted.get(key) is not None and trusted[key] != snapshot.get('enabled' if key == 'service_enabled' else 'active'):
                            self._checked(['systemctl', yes if trusted[key] else no, unit])
                verification = self._recipes().verify_plan(self.host, trusted)
                self._save_recipe(trusted)
                journal['recipe_after_sha256'] = _digest(self.host.path(f'{RECIPES}/{module}.json').read_bytes())
                journal['restore_guard'] = self.plan(module, trusted['values'], trusted.get('service_active'), trusted.get('service_enabled'))['fingerprint']
                journal['status'] = 'APPLIED'
                self._journal(journal)
            except Exception as apply_error:
                try:
                    journal['status'] = 'ROLLING_BACK'
                    self._journal(journal)
                    self._restore_files(journal)
                    for command in journal['restore_commands']:
                        self._checked(command)
                    for command in journal.get('validate', []):
                        self._checked(command)
                    if journal['files'] or journal.get('service_changed'):
                        self._service_restore(snapshot)
                    self._restore_recipe(journal)
                    journal['status'] = 'ROLLED_BACK'
                    self._journal(journal)
                    self._log_result({'status': 'ROLLED_BACK', 'module': module, 'transaction_id': identifier,
                                      'message': 'Reversible changes restored after checked failure.'})
                except Exception:
                    journal['status'] = 'RECOVERY_REQUIRED'
                    self._journal(journal)
                    raise RootActionError(f'Apply and rollback failed; inspect {BACKUPS}/{identifier} and {TRANSACTIONS}/{identifier}.', 'RECOVERY_REQUIRED') from None
                from .root_recipes import PHPVerificationError, SSHVerificationError
                detail = (' ' + apply_error.safe_detail) if isinstance(apply_error, (PHPVerificationError, SSHVerificationError)) else ''
                raise RootActionError(f'Apply failed; reversible files and service state were restored. Transaction {identifier}.{detail}', 'ROLLED_BACK') from None
            return self._log_result({'status': 'APPLIED', 'module': module, 'transaction_id': identifier,
                    'message': 'Selected configuration applied and saved.', 'verification': verification})

    def _restore_files(self, journal):
        identifier = journal['transaction_id']
        metadata_path = self.host.path(f'{BACKUPS}/{identifier}/metadata.json')
        self._trusted_file(metadata_path)
        metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
        for entry in journal['files']:
            target = self.host.path(entry['path'])
            current_hash = _digest(target.read_bytes() if target.exists() else None)
            if current_hash not in {entry['before_sha256'], entry['after_sha256']}:
                raise RootActionError('An external edit during the transaction prevents automatic rollback.', 'EXTERNAL_EDIT')
        for entry in reversed(journal['files']):
            target = self.host.path(entry['path'])
            if entry['before_sha256'] is None:
                if target.exists():
                    target.unlink()
            else:
                if not re.fullmatch(r'[0-9]{4}\.bin', entry['backup']):
                    raise RootActionError('Invalid backup reference.')
                backup = self.host.path(f"{BACKUPS}/{identifier}/{entry['backup']}")
                self._trusted_file(backup)
                content = backup.read_bytes()
                if _digest(content) != entry['before_sha256']:
                    raise RootActionError('Backup integrity check failed.')
                self._atomic(entry['path'], content, metadata[entry['path']])

    def _restore_candidate(self, module):
        self._module(module)
        self._check_pending()
        candidates = [j for j in self._journals() if j.get('module') == module and j.get('kind') == 'configuration' and j.get('status') == 'APPLIED']
        if not candidates:
            raise RootActionError('There is no applied configuration transaction to restore.', 'NO_BACKUP')
        journal = candidates[-1]
        current = self.plan(module, journal['values'], journal.get('service_active'), journal.get('service_enabled'))
        if current['fingerprint'] != journal.get('restore_guard'):
            raise RootActionError('Configuration or service state changed outside this transaction; restore was blocked.', 'EXTERNAL_EDIT')
        recipe = self.host.path(f'{RECIPES}/{module}.json')
        if recipe.exists():
            self._trusted_file(recipe)
        if _digest(recipe.read_bytes() if recipe.exists() else None) != journal.get('recipe_after_sha256'):
            raise RootActionError('Saved recipe changed after this transaction; restore was blocked.', 'EXTERNAL_EDIT')
        for entry in journal['files']:
            path = self.host.path(entry['path'])
            if _digest(path.read_bytes() if path.exists() else None) != entry['after_sha256']:
                raise RootActionError('An external edit prevents restore.', 'EXTERNAL_EDIT')
        return journal

    def restore_plan(self, module):
        journal = self._restore_candidate(module)
        return self._restore_summary(journal)

    def _restore_summary(self, journal):
        observations = {}
        commands = []
        if journal['module'] == 'time':
            commands = journal.get('restore_commands', [])
            previous = journal.get('observations', {}).get('previous_timezone')
            if not previous:
                previous = next((argv[2] for argv in commands if len(argv) == 3 and argv[:2] == ['timedatectl', 'set-timezone']), None)
            if previous:
                observations['previous_timezone'] = previous
        result = {'schema': 1, 'module': journal['module'], 'transaction_id': journal['transaction_id'],
                  'files': [{'path': entry['path'], 'before_sha256': entry['after_sha256'],
                             'after_sha256': entry['before_sha256']} for entry in journal['files']],
                  'service_snapshot': journal['service_snapshot'],
                  'observations': observations, 'restore_commands': commands,
                  'notes': ['Restore original file bytes and metadata, previous selected recipe and service state.',
                            'Installed packages and application data are not reverted.']}
        result['fingerprint'] = _fingerprint(result)
        return result

    def restore_last(self, module, expected_fingerprint=None):
        self._gate(module)
        with self._lock():
            journal = self._restore_candidate(module)
            if expected_fingerprint is not None and self._restore_summary(journal)['fingerprint'] != expected_fingerprint:
                raise RootActionError('Restore preview is stale; review the current transaction again.', 'STALE_PLAN')
            self._preflight_state()
            journal['status'] = 'RESTORING'
            self._journal(journal)
            try:
                self._restore_files(journal)
                for command in journal.get('restore_commands', []):
                    self._checked(command)
                for command in journal.get('validate', []):
                    self._checked(command)
                if journal['files'] or journal.get('service_changed'):
                    self._service_restore(journal['service_snapshot'])
                self._restore_recipe(journal)
            except Exception:
                journal['status'] = 'RECOVERY_REQUIRED'
                self._journal(journal)
                raise RootActionError(f'Restore failed; inspect {BACKUPS}/{journal["transaction_id"]}.', 'RECOVERY_REQUIRED') from None
            journal['status'] = 'RESTORED'
            self._journal(journal)
            return self._log_result({'status': 'RESTORED', 'module': module, 'transaction_id': journal['transaction_id'],
                    'message': 'Original file bytes, metadata and service state restored.',
                    'verification': {'verified': False, 'scope': 'configuration and service restoration', 'detail': 'Original recipe restored; application health was not revalidated.'}})

    def apt_plan(self, operation):
        from .root_apt import plan
        return plan(self, operation)

    def apt_run(self, preview):
        from .root_apt import run
        return run(self, preview)

    def mariadb_security_status(self):
        from .root_mariadb import status
        return status(self)

    def mariadb_security_plan(self, remote_enabled=None, change_password=False):
        from .root_mariadb import plan
        return plan(self, remote_enabled, change_password)

    def mariadb_security_apply(self, preview, password=None):
        from .root_mariadb import apply
        return apply(self, preview, password)

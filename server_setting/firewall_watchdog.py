"""Standalone stdlib firewall guard, copied privately before any kernel change.

This module intentionally imports no application code or virtualenv dependency.
Only root-owned system services run its mutation modes. The UI process is not
needed for expiry, rollback or boot recovery.
"""
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time

try:
    import fcntl
except ImportError:
    fcntl = None

TABLE = 'threewa_services'
OWNER = '3waServerSetup firewall schema2'
STATE = '/var/lib/3wa-server-setup/firewall'
TRANSACTIONS = '/var/lib/3wa-server-setup/transactions'
CONFIRMED = STATE + '/confirmed.json'
BOOT_UNIT = 'threewa-firewall-boot.service'
FINAL = {'FW_CONFIRMED', 'FW_ROLLED_BACK', 'FW_BOOT_ROLLED_BACK', 'FW_RESTORED_INITIAL', 'FW_RESTORED_PREVIOUS'}
ID = re.compile(r'[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}')
SAFE_PATH = '/usr/sbin:/usr/bin:/sbin:/bin'


class FirewallError(RuntimeError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True) + '\n').encode()


def normalized(value):
    if isinstance(value, list):
        return [normalized(item) for item in value]
    if isinstance(value, dict):
        return {key: normalized(item) for key, item in value.items() if key not in {'handle', 'packets', 'bytes'}}
    return value


def now():
    return time.clock_gettime(time.CLOCK_BOOTTIME) if hasattr(time, 'CLOCK_BOOTTIME') else time.monotonic()


def ticks():
    return int(now() * (os.sysconf('SC_CLK_TCK') if hasattr(os, 'sysconf') else 100))


def native_run(argv, timeout=15):
    if not argv or argv[0] not in {'nft', 'systemctl'}:
        raise FirewallError('Unsupported guard command.')
    executable = shutil.which(argv[0], path=SAFE_PATH)
    if not executable:
        raise FirewallError('Required guard command is unavailable.')
    resolved = Path(executable).resolve(strict=True)
    for path in [resolved, *resolved.parents]:
        info = path.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise FirewallError('Guard executable is not root controlled.')
    return subprocess.run([executable, *argv[1:]], stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, encoding='utf-8', errors='strict', check=False, timeout=timeout,
                          shell=False, env={'PATH': SAFE_PATH, 'HOME': '/root', 'LC_ALL': 'C', 'LANG': 'C'})


def connection_proof(pid=None):
    """Prove the SSH transport from ancestor socket FDs; environment is irrelevant."""
    caller = int(pid or os.getpid())
    if caller <= 1:
        raise FirewallError('A real SSH caller is required.')
    def process(number):
        directory = Path('/proc') / str(number)
        text = (directory / 'stat').read_text()
        fields = text[text.rfind(')') + 2:].split()
        return int(fields[1]), int(fields[19]), directory
    _, caller_start, _ = process(caller)
    current, candidates, visited = caller, [], set()
    binaries = {'/usr/sbin/sshd', '/usr/lib/openssh/sshd-session', '/usr/lib/openssh/sshd-auth'}
    while current > 1 and current not in visited and len(visited) < 64:
        visited.add(current)
        parent, started, directory = process(current)
        try:
            executable = os.readlink(directory / 'exe')
        except OSError:
            executable = ''
        if executable in binaries:
            executable_path = Path(executable).resolve(strict=True)
            for part in [executable_path, *executable_path.parents]:
                info = part.stat()
                if info.st_uid != 0 or info.st_mode & 0o022:
                    raise FirewallError('SSH transport executable is not root controlled.')
            inodes = set()
            for descriptor in (directory / 'fd').iterdir():
                try:
                    target = os.readlink(descriptor)
                except OSError:
                    continue
                match = re.fullmatch(r'socket:\[([0-9]+)\]', target)
                if match:
                    inodes.add(match[1])
            for filename, family in [('tcp', 4), ('tcp6', 6)]:
                for line in (directory / 'net' / filename).read_text().splitlines()[1:]:
                    fields = line.split()
                    if len(fields) < 10 or fields[3] != '01' or fields[9] not in inodes:
                        continue
                    def endpoint(value):
                        address, port = value.split(':')
                        raw = bytes.fromhex(address)
                        raw = raw[::-1] if family == 4 else b''.join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
                        address = ipaddress.ip_address(raw)
                        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
                            address = address.ipv4_mapped
                        return str(address), int(port, 16)
                    server, server_port = endpoint(fields[1])
                    peer, peer_port = endpoint(fields[2])
                    candidates.append({'caller_pid': caller, 'caller_start_ticks': caller_start,
                        'transport_pid': current, 'transport_start_ticks': started, 'socket_inode': fields[9],
                        'peer_ip': peer, 'peer_port': peer_port, 'server_ip': server, 'server_port': server_port,
                        'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()})
        current = parent
    if not candidates:
        raise FirewallError('No verifiable SSH transport was found; no environment fallback is allowed.')
    # Privsep siblings can share one transport. The oldest transport holder is
    # used so a new channel on an old ControlMaster cannot look like a new SSH.
    sockets = {item['socket_inode'] for item in candidates}
    if len(sockets) != 1:
        raise FirewallError('Ambiguous SSH transport ancestry.')
    return min(candidates, key=lambda item: item['transport_start_ticks'])


class Runtime:
    def __init__(self, root=None, run=None):
        self.root = Path(root) if root is not None else Path('/')
        self.run = run or native_run

    def path(self, absolute):
        if not absolute.startswith('/') or any(part in {'.', '..'} for part in absolute.split('/')) or '\\' in absolute:
            raise FirewallError('Unsafe guard path.')
        path = self.root
        for part in ['', *absolute.split('/')[1:]]:
            path = path / part
            if path.is_symlink():
                raise FirewallError('Symbolic links are not accepted in guard state.')
        return path

    def ensure_dir(self, absolute, private=True):
        path = self.path(absolute)
        missing, current = [], path
        while not current.exists():
            missing.append(current)
            current = current.parent
        for ancestor in [current, *current.parents]:
            info = ancestor.stat()
            if not stat.S_ISDIR(info.st_mode) or (os.name == 'posix' and (info.st_uid != 0 or info.st_mode & 0o022)):
                raise FirewallError('Guard directory is not root controlled.')
            if ancestor == self.root:
                break
        for directory in reversed(missing):
            directory.mkdir(mode=0o700 if private else 0o755)
        return path

    def trusted(self, path):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or (os.name == 'posix' and (info.st_uid != 0 or info.st_mode & 0o077)):
            raise FirewallError('Guard state is not private to root.')

    def read_json(self, absolute, default=None):
        path = self.path(absolute)
        if not path.exists():
            return default
        self.trusted(path)
        if path.stat().st_size > 4 * 1024 * 1024:
            raise FirewallError('Guard state exceeds its size limit.')
        return json.loads(path.read_text(encoding='utf-8'))

    def atomic(self, absolute, data):
        path = self.path(absolute)
        self.ensure_dir('/' + '/'.join(absolute.split('/')[1:-1]))
        if path.exists():
            self.trusted(path)
        descriptor, temporary = tempfile.mkstemp(prefix='.guard-', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                if os.name == 'posix':
                    os.fchmod(stream.fileno(), 0o600)
            self.path(absolute)
            os.replace(temporary, path)
            if os.name == 'posix':
                fd = os.open(path.parent, os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def write_json(self, absolute, value):
        self.atomic(absolute, encoded(value))

    def directory(self, identifier):
        if not isinstance(identifier, str) or not ID.fullmatch(identifier):
            raise FirewallError('Invalid firewall transaction identity.')
        return f'{TRANSACTIONS}/{identifier}'

    def journal(self, identifier):
        journal = self.read_json(self.directory(identifier) + '/journal.json')
        if not journal or journal.get('kind') != 'firewall' or journal.get('transaction_id') != identifier:
            raise FirewallError('Firewall transaction is missing or invalid.')
        return journal

    def write_journal(self, journal):
        self.write_json(self.directory(journal['transaction_id']) + '/journal.json', journal)

    def checked(self, argv, timeout=15):
        result = self.run(argv, timeout=timeout)
        if result.returncode:
            raise FirewallError('A checked firewall/systemd command failed.')
        return result.stdout

    def boot_id(self):
        return self.path('/proc/sys/kernel/random/boot_id').read_text().strip()

    def proof(self, pid=None):
        return connection_proof(pid)

    @contextmanager
    def lock(self):
        if fcntl is None:
            raise FirewallError('Native flock is required for firewall mutation.')
        self.ensure_dir(STATE)
        path = self.path(STATE + '/operation.lock')
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            self.trusted(path)
            deadline = time.monotonic() + 3
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise FirewallError('Firewall worker lock did not become available.')
                    time.sleep(.05)
            yield
        finally:
            os.close(descriptor)

    def snapshot(self):
        data = json.loads(self.checked(['nft', '-j', '-a', 'list', 'ruleset'], timeout=8))
        if not isinstance(data.get('nftables'), list):
            raise FirewallError('Unrecognized nftables ruleset.')
        own, foreign, table, foreign_hooks = [], [], None, []
        for item in data['nftables']:
            if not isinstance(item, dict) or len(item) != 1:
                raise FirewallError('Unrecognized nftables object.')
            kind, value = next(iter(item.items()))
            if kind == 'metainfo':
                continue
            belongs = value.get('family') == 'inet' and (value.get('table') == TABLE or kind == 'table' and value.get('name') == TABLE)
            (own if belongs else foreign).append(item)
            if not belongs and kind == 'chain' and value.get('hook'):
                foreign_hooks.append({key: value.get(key) for key in ('family', 'table', 'name', 'hook')})
            if belongs and kind == 'table':
                table = value
        return {'exists': table is not None, 'handle': table.get('handle') if table else None,
                'comment': table.get('comment') if table else None,
                'sha256': digest(encoded(normalized(own))),
                'foreign_sha256': digest(encoded(normalized(foreign))), 'foreign_hooks': foreign_hooks}

    def expected(self, snapshot, expected):
        return (snapshot['exists'] == expected['exists'] and snapshot['sha256'] == expected['sha256']
                and snapshot['handle'] == expected['handle'])

    def policy_matches(self, snapshot, expected):
        return (snapshot['exists'] == expected['exists'] and snapshot['sha256'] == expected['sha256']
                and snapshot.get('comment') == expected.get('comment'))

    def unit(self, role, identifier):
        self.directory(identifier)
        return f'threewa-fw-{role}@{identifier}.service'

    def files_valid(self, journal):
        directory = self.directory(journal['transaction_id'])
        for name, expected in journal['file_hashes'].items():
            if name not in {'candidate.nft', 'boot.nft', 'before.nft', 'watchdog.py'}:
                raise FirewallError('Unexpected immutable guard artifact.')
            path = self.path(directory + '/' + name)
            self.trusted(path)
            if digest(path.read_bytes()) != expected:
                raise FirewallError('An immutable firewall artifact changed.')

    def arm(self, identifier):
        with self.lock():
            journal = self.journal(identifier)
            self.files_valid(journal)
            if journal['status'] in FINAL:
                return journal
            if journal['boot_id'] != self.boot_id():
                raise FirewallError('Firewall guard belongs to another boot.')
            if journal['status'] == 'FW_PREPARED':
                journal.update(status='FW_ARMED', deadline_boottime=now() + journal['timeout_seconds'])
                self.write_journal(journal)
            return journal

    def apply(self, identifier):
        with self.lock():
            journal = self.journal(identifier)
            self.files_valid(journal)
            if journal['status'] != 'FW_ARMED' or now() >= journal['deadline_boottime'] - 5:
                raise FirewallError('A live, armed firewall guard is required.')
            current = self.snapshot()
            if not self.expected(current, journal['before']) or current['foreign_sha256'] != journal['before']['foreign_sha256']:
                raise FirewallError('Firewall state changed after preview.')
            journal['status'] = 'FW_APPLYING'
            self.write_journal(journal)
            self.checked(['nft', '-f', str(self.path(self.directory(identifier) + '/candidate.nft'))], timeout=8)
            after = self.snapshot()
            if after['comment'] != OWNER + ' generation=' + journal['generation'] or after['foreign_sha256'] != current['foreign_sha256']:
                raise FirewallError('Firewall kernel ownership verification failed.')
            journal.update(status='FW_AWAITING_NEW_SSH', after=after, applied_ticks=ticks())
            self.write_journal(journal)

    def confirm(self, identifier):
        with self.lock():
            journal = self.journal(identifier)
            self.files_valid(journal)
            request = self.read_json(self.directory(identifier) + '/confirm-request.json')
            if journal['status'] != 'FW_AWAITING_NEW_SSH' or now() >= journal['deadline_boottime']:
                raise FirewallError('Firewall confirmation expired or is no longer pending.')
            actual = self.proof(request['caller_pid'])
            origin = journal['origin']
            if (actual != request or actual['boot_id'] != journal['boot_id']
                    or actual['socket_inode'] == origin['socket_inode']
                    or all(actual[key] == origin[key] for key in ('peer_ip', 'peer_port', 'server_ip', 'server_port'))
                    or actual['transport_start_ticks'] <= journal['applied_ticks']
                    or actual['server_port'] != journal['rescue']['port']
                    or not any(ipaddress.ip_address(actual['peer_ip']) in ipaddress.ip_network(source) for source in journal['rescue']['sources'])):
                raise FirewallError('A new verified SSH transport after rule application is required.')
            if not self.expected(self.snapshot(), journal['after']):
                raise FirewallError('Applied firewall rules changed externally.')
            committed = {'schema': 1, 'transaction_id': identifier, 'generation': journal['generation'],
                         'config': journal['config'], 'rescue': journal['rescue'],
                         'script': self.path(self.directory(identifier) + '/boot.nft').read_text(),
                         'owned': journal['after']}
            self.write_json(CONFIRMED, committed)
            self.checked(['systemctl', 'enable', BOOT_UNIT], timeout=8)
            journal.update(status='FW_CONFIRMED', confirmed_sha256=digest(encoded(committed)), confirmation=actual)
            self.write_journal(journal)

    def expire(self, identifier):
        # Stop bounded mutation cgroups before taking the firewall lock. The UI
        # and global RootManager lock are deliberately irrelevant here.
        self.checked(['systemctl', 'stop', self.unit('apply', identifier), self.unit('confirm', identifier)], timeout=8)
        return self.rollback(identifier)

    def rollback(self, identifier, manual=False, boot=False):
        with self.lock():
            journal = self.journal(identifier)
            if journal['status'] in {'FW_ROLLED_BACK', 'FW_BOOT_ROLLED_BACK', 'FW_RESTORED_INITIAL', 'FW_RESTORED_PREVIOUS'}:
                return journal
            if journal['status'] == 'FW_CONFIRMED':
                if not manual:
                    return journal
                committed = self.read_json(CONFIRMED)
                if not committed or committed['transaction_id'] != identifier:
                    raise FirewallError('Only the latest confirmed transaction can be restored.')
                if journal['before']['exists']:
                    request = self.read_json(self.directory(identifier) + '/rollback-request.json')
                    actual = self.proof(request['caller_pid'])
                    rescue = journal['before_committed']['rescue']
                    if actual != request or actual['server_port'] != rescue['port'] or not any(
                            ipaddress.ip_address(actual['peer_ip']) in ipaddress.ip_network(source) for source in rescue['sources']):
                        raise FirewallError('The current SSH peer is not covered by the previous confirmed rescue policy.')
            current = self.snapshot()
            candidate = current['comment'] == OWNER + ' generation=' + journal['generation']
            if not candidate and not self.policy_matches(current, journal['before']) and not (boot and not current['exists']):
                raise FirewallError('An external firewall edit blocks automatic rollback.')
            if candidate and journal.get('after'):
                matches = (self.policy_matches(current, journal['after']) if journal['status'] == 'FW_CONFIRMED' or boot
                           else self.expected(current, journal['after']))
                if not matches:
                    raise FirewallError('An external firewall edit blocks automatic rollback.')
            commands = []
            if candidate:
                commands.append(f'delete table inet handle {current["handle"]}')
            if journal['before']['exists'] and (candidate or boot and not current['exists']):
                commands.append(self.path(self.directory(identifier) + '/before.nft').read_text())
            if commands:
                path = self.directory(identifier) + '/rollback.nft'
                self.atomic(path, ('\n'.join(commands) + '\n').encode())
                self.checked(['nft', '-f', str(self.path(path))], timeout=8)
            if self.snapshot()['sha256'] != journal['before']['sha256']:
                raise FirewallError('Original owned firewall policy was not restored.')
            if journal['before_committed'] is None:
                path = self.path(CONFIRMED)
                if path.exists():
                    self.trusted(path)
                    path.unlink()
            else:
                self.write_json(CONFIRMED, journal['before_committed'])
            self.checked(['systemctl', 'enable' if journal['boot_enabled_before'] else 'disable', BOOT_UNIT], timeout=8)
            journal['status'] = (('FW_RESTORED_PREVIOUS' if journal['before']['exists'] else 'FW_RESTORED_INITIAL') if manual and journal.get('confirmed_sha256') else
                                 'FW_BOOT_ROLLED_BACK' if boot else 'FW_ROLLED_BACK')
            self.write_journal(journal)
            return journal

    def boot(self):
        directory = self.path(TRANSACTIONS)
        if directory.exists():
            for child in sorted(directory.iterdir()):
                if not ID.fullmatch(child.name):
                    continue
                journal = self.read_json(f'{TRANSACTIONS}/{child.name}/journal.json')
                if journal is None:
                    continue
                if journal.get('kind') == 'firewall' and journal['status'] not in FINAL:
                    self.rollback(child.name, boot=True)
        with self.lock():
            self._load_confirmed()

    def _load_confirmed(self):
        committed = self.read_json(CONFIRMED)
        if committed is None:
            return
        journal = self.journal(committed['transaction_id'])
        if journal['status'] != 'FW_CONFIRMED' or digest(encoded(committed)) != journal['confirmed_sha256']:
            raise FirewallError('Only a confirmed persistent firewall may load at boot.')
        current = self.snapshot()
        if current['exists']:
            if current['sha256'] != committed['owned']['sha256']:
                raise FirewallError('Boot found an unrecognized same-name firewall table.')
            return
        path = STATE + '/boot-load.nft'
        self.atomic(path, committed['script'].encode())
        self.checked(['nft', '-f', str(self.path(path))], timeout=8)
        if self.snapshot()['sha256'] != committed['owned']['sha256']:
            raise FirewallError('Confirmed boot firewall verification failed.')


def notify(message):
    address = os.environ.get('NOTIFY_SOCKET')
    if not address:
        raise FirewallError('A systemd notification socket is required.')
    if address.startswith('@'):
        address = '\0' + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
        channel.connect(address)
        channel.sendall(message.encode())


def main():
    if os.name != 'posix' or os.geteuid() != 0:
        raise FirewallError('Native root firewall service is required.')
    mode = sys.argv[1] if len(sys.argv) > 1 else ''
    runtime = Runtime()
    if mode == 'boot' and len(sys.argv) == 2:
        runtime.boot()
        return
    if len(sys.argv) != 3 or mode not in {'guard', 'apply', 'confirm', 'rollback'}:
        raise FirewallError('Invalid firewall service invocation.')
    identifier = sys.argv[2]
    if mode == 'guard':
        runtime.arm(identifier)
        notify('READY=1')
        while True:
            journal = runtime.journal(identifier)
            if journal['status'] in FINAL:
                return
            notify('WATCHDOG=1')
            if journal['boot_id'] != runtime.boot_id() or now() >= journal['deadline_boottime']:
                runtime.expire(identifier)
                return
            time.sleep(.25)
    elif mode == 'apply':
        runtime.apply(identifier)
    elif mode == 'confirm':
        runtime.confirm(identifier)
    else:
        runtime.expire(identifier) if runtime.journal(identifier)['status'] != 'FW_CONFIRMED' else runtime.rollback(identifier, manual=True)


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        print('Guarded firewall operation failed; inspect private transaction state.', file=sys.stderr)
        raise SystemExit(1)

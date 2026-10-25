"""Native ext4 swap files, with retained inodes until activation is verified.

Swap contents are paging data, never a configuration backup. Successful resize
removes the inactive original; incomplete transactions retain it for recovery.
"""
import errno
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time

from .root_actions import (BACKUPS, TRANSACTIONS, Host, RootManager, RootActionError,
                           TRUSTED_PATH, _digest, _fingerprint)


MIB = 1024 * 1024
RESERVE_BYTES = 1024 * MIB
MEMORY_MARGIN_BYTES = 256 * MIB
SAVED = '/opt/3waServerSetting/config/swap/draft.json'
OWNED = '/opt/3waServerSetting/config/swap/owned.json'
STAGING = '/.3wa-swap'
PATHS = {'/swapfile', '/swap.img'}
_COMMANDS = {'findmnt', 'blkid', 'mkswap', 'swapon', 'swapoff', 'systemctl'}


def normalize(config):
    if (not isinstance(config, dict)
            or set(config) != {'schema', 'path', 'size_mib', 'active', 'persistent'}
            or type(config['schema']) is not int or config['schema'] != 1
            or not isinstance(config['path'], str) or config['path'] not in PATHS or type(config['size_mib']) is not int
            or not 16 <= config['size_mib'] <= 1048576
            or type(config['active']) is not bool or type(config['persistent']) is not bool):
        raise RootActionError('A complete schema 1 swap configuration with a known path is required.', 'SWAP_INVALID')
    return dict(config)


class SwapHost(Host):
    """Only this boundary executes the fixed native swap tools."""

    def run(self, argv, timeout=30):
        if (not isinstance(argv, (tuple, list)) or not argv
                or argv[0] not in _COMMANDS
                or any(not isinstance(arg, str) or '\x00' in arg for arg in argv)
                or not isinstance(timeout, (int, float)) or not 0 < timeout <= 900):
            raise RootActionError('Invalid bounded swap command.', 'UNSAFE_COMMAND')
        if self.root != Path('/').absolute() or getattr(os, 'geteuid', lambda: -1)() != 0:
            raise RootActionError('Native swap commands require the local privileged host.', 'ROOT_REQUIRED')
        from .trusted_commands import resolve
        executable = resolve(argv[0])
        if executable is None:
            raise RootActionError('A trusted native swap command is unavailable.', 'MISSING_COMMAND')
        env = {'PATH': TRUSTED_PATH, 'LC_ALL': 'C', 'LANG': 'C', 'HOME': '/root',
               'SYSTEMD_PAGER': 'cat'}
        try:
            result = subprocess.run([executable, *argv[1:]], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    errors='replace', timeout=timeout, env=env, shell=False, check=False)
        except subprocess.TimeoutExpired:
            raise RootActionError(f'{argv[0]} timed out.', 'TIMEOUT') from None
        except OSError:
            raise RootActionError(f'{argv[0]} could not execute.', 'COMMAND_FAILED') from None
        if len(result.stdout) + len(result.stderr) > MIB:
            raise RootActionError('Native swap command output exceeded its bound.', 'COMMAND_FAILED')
        return result

    def free_bytes(self):
        return shutil.disk_usage(self.path('/')).free

    def allocated(self, absolute, known_good=False):
        """ext4 reports holes explicitly; no fallocate/sparse approximation."""
        target = self.path(absolute)
        info = target.stat()
        if not hasattr(os, 'SEEK_HOLE') or not hasattr(info, 'st_blocks'):
            raise RootActionError('Native allocated-extent inspection is unavailable.', 'SWAP_UNSUPPORTED')
        if info.st_blocks * 512 < info.st_size:
            return False
        if known_good:
            # ext4 installer files may have fully allocated UNWRITTEN extents,
            # which SEEK_HOLE reports as holes. Kernel acceptance, or that
            # exact inode+signature retained in private state, is stronger
            # evidence than this logical-data probe. Sparse block counts are
            # still rejected above.
            return True
        descriptor = os.open(target, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            try:
                return os.lseek(descriptor, 0, os.SEEK_HOLE) >= info.st_size
            except OSError as error:
                # Active swap inodes reject some extent operations. st_blocks
                # is sufficient on the explicitly gated, non-CoW ext4 scope.
                if error.errno == errno.EINVAL:
                    return info.st_blocks * 512 >= info.st_size
                raise RootActionError('Cannot inspect swap extents.', 'SWAP_UNSUPPORTED') from None
        finally:
            os.close(descriptor)


class SwapManager:
    def __init__(self, host=None):
        self.host = host or SwapHost()
        self._root = RootManager(self.host)

    def _gate(self):
        self._root._gate('time')

    def _bytes(self, absolute, limit=MIB, required=False):
        target = self.host.path(absolute)
        try:
            if not stat.S_ISREG(target.stat().st_mode):
                raise RootActionError('Swap input is not a regular file.', 'SWAP_UNSUPPORTED')
            with target.open('rb') as stream:
                data = stream.read(limit + 1)
            if len(data) > limit:
                raise RootActionError('Swap input exceeds the supported size.', 'SWAP_UNSUPPORTED')
            return data
        except FileNotFoundError:
            if required:
                raise RootActionError('A required native swap input is unavailable.', 'SWAP_UNSUPPORTED') from None
            return None

    def _state_json(self, absolute, default=None):
        data = self._bytes(absolute)
        if data is None:
            return default
        self._root._trusted_file(self.host.path(absolute))
        try:
            return json.loads(data.decode('utf-8'))
        except (UnicodeError, ValueError):
            raise RootActionError('Saved swap state is invalid.', 'UNSAFE_STATE') from None

    def load_saved(self):
        value = self._state_json(SAVED)
        return normalize(value) if value is not None else None

    def save(self, config):
        config = normalize(config)
        self._gate()
        with self._root._lock():
            self._root._check_pending()
            self._root._secure_dir('/opt/3waServerSetting/config/swap')
            self._root._private_json(SAVED, config)
        return {'status': 'SAVED', 'module': 'swap', 'config': config,
                'message': 'Swap recipe saved; the active swap and fstab were not changed.'}

    def _checked(self, argv, timeout=30):
        result = self.host.run(argv, timeout=timeout)
        if result.returncode:
            raise RootActionError(f'{argv[0]} failed; native diagnostic output was discarded.', 'SWAP_COMMAND_FAILED')
        return result.stdout

    def _swaps(self):
        raw = self._bytes('/proc/swaps', required=True).decode('ascii', errors='strict')
        rows = raw.splitlines()
        if not rows or rows[0].split() != ['Filename', 'Type', 'Size', 'Used', 'Priority']:
            raise RootActionError('Unknown kernel swap listing.', 'SWAP_UNSUPPORTED')
        result = {}
        for line in rows[1:]:
            fields = line.split()
            if (len(fields) != 5 or not fields[0].startswith('/') or fields[0] in result
                    or not fields[2].isdigit() or not fields[3].isdigit()):
                raise RootActionError('Unknown kernel swap entry.', 'SWAP_UNSUPPORTED')
            result[fields[0]] = {'type': fields[1], 'size_bytes': int(fields[2]) * 1024,
                                 'used_bytes': int(fields[3]) * 1024}
        return result

    def _memory(self):
        data = self._bytes('/proc/meminfo', required=True)
        found = re.findall(rb'^MemAvailable:\s+([0-9]+) kB\r?$', data, re.M)
        if len(found) != 1:
            raise RootActionError('MemAvailable is unavailable.', 'SWAP_MEMORY')
        return int(found[0]) * 1024

    def _off_guard(self, path):
        active = self._swaps().get(path)
        if active is not None and self._memory() < active['used_bytes'] + MEMORY_MARGIN_BYTES:
            raise RootActionError('MemAvailable must exceed selected swap usage plus 256 MiB before swapoff.', 'SWAP_MEMORY')

    def _identity(self, path, known_identity=None):
        target = self.host.path(path)
        self._root._trusted_target(target)
        if not target.exists():
            return None
        info = target.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or (os.name == 'posix' and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600))
                or info.st_size < 16 * MIB):
            raise RootActionError('Existing swap must be root-owned mode 0600, one link, and fully allocated.', 'SWAP_UNSUPPORTED')
        output = self._checked(['blkid', '--probe', '--match-tag', 'TYPE', '--match-tag', 'UUID',
                                '--match-tag', 'LABEL', '--output', 'export', '--', path])
        tags = {}
        for line in output.splitlines():
            key, separator, value = line.partition('=')
            if not separator or key in tags or key not in {'DEVNAME', 'TYPE', 'UUID', 'LABEL'}:
                raise RootActionError('Unknown swap signature.', 'SWAP_UNSUPPORTED')
            tags[key] = value
        if tags.get('DEVNAME') != path or tags.get('TYPE') != 'swap' or not tags.get('UUID'):
            raise RootActionError('Existing file has no unambiguous native swap signature.', 'SWAP_UNSUPPORTED')
        identity = {'device': info.st_dev, 'inode': info.st_ino, 'size_bytes': info.st_size,
                    'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid,
                    'nlink': info.st_nlink, 'uuid': tags['UUID'], 'label': tags.get('LABEL', '')}
        if known_identity is None and path in PATHS:
            owned = self._state_json(OWNED, {})
            if isinstance(owned, dict):
                known_identity = owned.get(path)
        active = self._swaps().get(path)
        kernel_proven = (active is not None and active['type'] == 'file'
                         and 0 < info.st_size - active['size_bytes'] <= MIB)
        if not self.host.allocated(path, known_good=kernel_proven or known_identity == identity):
            raise RootActionError('Swap allocation cannot be proven; unknown inactive sparse or unwritten layouts are unsupported.', 'SWAP_UNSUPPORTED')
        return identity

    def _fstab(self, path, identity, persistent=None):
        self._root._trusted_target(self.host.path('/etc/fstab'))
        data = self._bytes('/etc/fstab', required=True)
        lines = data.splitlines(keepends=True)
        selected = []
        for index, line in enumerate(lines):
            fields = line.partition(b'#')[0].split()
            if not fields:
                continue
            source = fields[0]
            if identity is not None and source in {
                    ('UUID=' + identity['uuid']).encode(),
                    ('LABEL=' + identity['label']).encode() if identity['label'] else b'\x00'}:
                raise RootActionError('Swap UUID/LABEL aliases are unsupported; use one plain fstab path.', 'SWAP_UNSUPPORTED')
            if source != path.encode('ascii'):
                continue
            if (len(fields) != 6 or fields[1:3] != [b'none', b'swap']
                    or fields[4:] != [b'0', b'0']
                    or not set(fields[3].split(b',')) <= {b'sw', b'defaults', b'nofail'}):
                raise RootActionError('Only a plain native swap fstab entry is supported.', 'SWAP_UNSUPPORTED')
            selected.append(index)
        if len(selected) > 1:
            raise RootActionError('Duplicate swap fstab entries are unsupported.', 'SWAP_UNSUPPORTED')
        if persistent is None:
            return data, bool(selected), data
        newline = b'\r\n' if b'\r\n' in data else b'\n'
        replacement = path.encode('ascii') + b' none swap sw,nofail 0 0' + newline
        if selected:
            lines[selected[0]] = replacement if persistent else b''
        elif persistent:
            if data and not data.endswith((b'\n', b'\r')):
                lines.append(newline)
            lines.append(replacement)
        return data, bool(selected), b''.join(lines)

    def _unit(self, path):
        unit = path[1:] + '.swap'
        argv = ['systemctl', 'show', '--all', '--property=LoadState', '--property=FragmentPath',
                '--property=SourcePath', '--property=DropInPaths', '--property=ActiveState',
                '--property=What', '--property=Transient', unit]
        result = self.host.run(argv, timeout=30)
        values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        required = {'LoadState', 'FragmentPath', 'SourcePath', 'DropInPaths', 'ActiveState', 'What', 'Transient'}
        if set(values) != required or values['DropInPaths'] or values['Transient'] != 'no':
            raise RootActionError('Unknown swap systemd unit layout.', 'SWAP_UNSUPPORTED')
        if values['LoadState'] == 'not-found' and not values['FragmentPath'] and not values['SourcePath']:
            return {'unit': unit, 'source': 'none'}
        kernel_active = path in self._swaps()
        if (not result.returncode and values['LoadState'] == 'loaded'
                and not values['FragmentPath'] and not values['SourcePath']
                and values['What'] == path
                and values['ActiveState'] == ('active' if kernel_active else 'inactive')):
            return {'unit': unit, 'source': 'kernel'}
        allowed = {f'/run/systemd/{directory}/{unit}' for directory in ('generator', 'generator.early', 'generator.late')}
        if (result.returncode or values['LoadState'] != 'loaded' or values['FragmentPath'] not in allowed
                or values['SourcePath'] != '/etc/fstab'):
            raise RootActionError('Custom systemd swap units are unsupported.', 'SWAP_UNSUPPORTED')
        return {'unit': unit, 'source': 'fstab'}

    def _observe(self, path):
        if path not in PATHS:
            raise RootActionError('Unsupported swap path.', 'SWAP_INVALID')
        self._gate()
        if self._checked(['findmnt', '--noheadings', '--output', 'FSTYPE', '--target', '/']).strip() != 'ext4':
            raise RootActionError('Swap file changes currently support native ext4 only.', 'SWAP_UNSUPPORTED')
        identity = self._identity(path)
        if identity is not None and identity['device'] != self.host.path('/').stat().st_dev:
            raise RootActionError('Selected swap must reside on the native root ext4 filesystem.', 'SWAP_UNSUPPORTED')
        swaps = self._swaps()
        selected = swaps.get(path)
        if selected is not None and (identity is None or selected['type'] != 'file'
                                     or not 0 < identity['size_bytes'] - selected['size_bytes'] <= MIB):
            raise RootActionError('Kernel swap path/size disagrees with the selected regular file.', 'SWAP_UNSUPPORTED')
        data, persistent, _ = self._fstab(path, identity)
        owned = self._state_json(OWNED, {})
        if not isinstance(owned, dict) or not set(owned) <= PATHS:
            raise RootActionError('Invalid swap ownership state.', 'UNSAFE_STATE')
        if identity is not None and not (selected or persistent or owned.get(path) == identity):
            raise RootActionError('Unknown existing file; adoption requires a matching native swap/fstab or tool identity.', 'SWAP_UNSUPPORTED')
        unit = self._unit(path)
        if unit['source'] == 'fstab' and not persistent:
            raise RootActionError('A stale generated swap unit needs administrator inspection.', 'SWAP_UNSUPPORTED')
        rc = self._bytes('/etc/rc.local') or b''
        for line in rc.splitlines():
            line = line.partition(b'#')[0]
            if re.search(rb'\b(?:swapon|swapoff|mkswap)\b', line) or path.encode() in line:
                raise RootActionError('rc.local swap management is unsupported.', 'SWAP_UNSUPPORTED')
        return {'path': path, 'identity': identity, 'active': selected is not None,
                'persistent': persistent, 'used_bytes': selected['used_bytes'] if selected else 0,
                'size_mib': identity['size_bytes'] // MIB if identity else 0,
                'fstab_sha256': _digest(data), 'unit': unit,
                'saved_sha256': _digest(self._bytes(SAVED)), 'owned_sha256': _digest(self._bytes(OWNED)),
                'other_active_paths': sorted(set(swaps) - {path})}

    def status(self, path=None):
        if path is None:
            path = next((name for name in ('/swapfile', '/swap.img') if self.host.path(name).exists()), '/swapfile')
        base = {'path': path, 'recommended_size_mib': 16384}
        try:
            value = self._observe(path)
            base.update({key: value[key] for key in ('size_mib', 'active', 'persistent', 'used_bytes', 'other_active_paths')})
            base.update({'supported': True, 'exists': value['identity'] is not None,
                         'free_bytes': self.host.free_bytes(), 'mem_available_bytes': self._memory(),
                         'detail': 'Native ext4 swap file; other swap areas are preserved.'})
        except RootActionError as error:
            base.update({'supported': False, 'code': error.code, 'detail': str(error)})
        return base

    def plan(self, config):
        config = normalize(config)
        state = self._observe(config['path'])
        replace = state['identity'] is None or state['size_mib'] != config['size_mib']
        if replace and self.host.free_bytes() < config['size_mib'] * MIB + RESERVE_BYTES:
            raise RootActionError('The full candidate swap plus 1 GiB free reserve is required; the old file is retained during validation.', 'SWAP_DISK_SPACE')
        if state['active'] and (replace or not config['active']):
            self._off_guard(config['path'])
        data, _, after = self._fstab(config['path'], state['identity'], config['persistent'])
        stable = {key: value for key, value in state.items() if key not in {'used_bytes'}}
        plan = {'schema': 1, 'module': 'swap', 'config': config, 'state_fingerprint': _fingerprint(stable),
                'replace_file': replace, 'fstab_before_sha256': _digest(data), 'fstab_after_sha256': _digest(after),
                'fstab_entry': config['path'] + ' none swap sw,nofail 0 0' if config['persistent'] else None,
                'fstab_action': ('unchanged' if data == after else ('set selected entry' if config['persistent'] else 'remove selected entry')),
                'observations': state,
                'notes': ['Only the selected swap path is changed; other swap areas are preserved.',
                          'A complete zero-filled candidate and 1 GiB free reserve are required before resize.',
                          'Successful resize clears the inactive old file; swap paging data is never restored.',
                          'Keeping the reported whole-MiB capacity preserves any existing fractional MiB and exact file bytes.',
                          'Boot persistence uses a plain fstab sw,nofail entry.']}
        plan['fingerprint'] = _fingerprint(plan)
        return plan

    def _verify(self, config):
        state = self._observe(config['path'])
        if (state['identity'] is None or state['size_mib'] != config['size_mib']
                or state['active'] != config['active'] or state['persistent'] != config['persistent']):
            raise RootActionError('Selected swap runtime/file/fstab verification failed.', 'SWAP_VERIFY_FAILED')
        return {'verified': True, 'scope': 'selected swap signature, file size, kernel activation and fstab persistence',
                'active': state['active'], 'persistent': state['persistent'], 'size_mib': state['size_mib']}

    def _sync_directory(self, target):
        if os.name == 'posix':
            descriptor = os.open(target, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _move(self, source, destination):
        source_path = self.host.path(source)
        target = self.host.path(destination)
        self._root._trusted_target(source_path)
        self._root._trusted_target(target)
        os.replace(source_path, target)
        self._sync_directory(source_path.parent)
        if target.parent != source_path.parent:
            self._sync_directory(target.parent)

    def _remove_inactive(self, path, expected):
        if path in self._swaps():
            raise RootActionError('An active swap file must never be removed.', 'RECOVERY_REQUIRED')
        if not self.host.path(path).exists():
            return
        if self._identity(path, expected) != expected:
            raise RootActionError('Swap file identity changed outside this transaction.', 'EXTERNAL_EDIT')
        self.host.path(path).unlink()
        self._sync_directory(self.host.path(path).parent)

    def _zero_fill(self, path, size):
        target = self.host.path(path)
        started = time.monotonic()
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                block = bytes(MIB)
                for index in range(size):
                    if index % 64 == 0 and time.monotonic() - started > 900:
                        raise RootActionError('Swap zero-fill exceeded its 900 second bound.', 'TIMEOUT')
                    stream.write(block)
                stream.flush()
                os.fsync(stream.fileno())
                if os.name == 'posix':
                    os.fchmod(stream.fileno(), 0o600)
        except Exception:
            # The private journal knows the staging path even before a valid
            # swap header exists; recovery can remove this incomplete inode.
            raise
        self._checked(['mkswap', '--', path], timeout=120)
        descriptor = os.open(target, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _set_active(self, path, active):
        current = path in self._swaps()
        if current == active:
            return
        if not active:
            self._off_guard(path)
        self._checked(['swapon' if active else 'swapoff', '--', path], timeout=120)
        if (path in self._swaps()) != active:
            raise RootActionError('Kernel swap activation state did not change as requested.', 'SWAP_VERIFY_FAILED')

    def _backup(self, journal, after_fstab):
        identifier = journal['transaction_id']
        self._root._secure_dir(f'{BACKUPS}/{identifier}')
        metadata = {}
        entries = []
        values = [('/etc/fstab', after_fstab), (SAVED, (json.dumps(journal['config'], sort_keys=True, ensure_ascii=False, separators=(',', ':')) + '\n').encode()),
                  (OWNED, self._bytes(OWNED))]
        for index, (absolute, after) in enumerate(values):
            before = self._bytes(absolute, required=absolute == '/etc/fstab')
            info = self._root._metadata(self.host.path(absolute))
            metadata[absolute] = info
            name = f'{index:04d}.bin'
            if before is not None:
                self._root._atomic(f'{BACKUPS}/{identifier}/{name}', before, {'mode': 0o600, 'uid': 0, 'gid': 0, 'xattrs': {}})
            entries.append({'path': absolute, 'before_sha256': _digest(before), 'after_sha256': _digest(after), 'backup': name})
        self._root._private_json(f'{BACKUPS}/{identifier}/metadata.json', metadata)
        journal['files'] = entries
        journal['fstab_metadata'] = metadata['/etc/fstab']

    def _guard_restore(self, journal):
        for entry in journal['files']:
            if _digest(self._bytes(entry['path'])) not in {entry['before_sha256'], entry['after_sha256']}:
                raise RootActionError('An external configuration edit prevents swap recovery.', 'EXTERNAL_EDIT')

    def _rollback(self, journal):
        self._guard_restore(journal)
        path = journal['config']['path']
        original = journal['before']['identity']
        candidate = journal.get('candidate_identity')
        current = self._identity(path, original)
        retained_path = journal['original_path']
        retained = self._identity(retained_path, original)
        if original is not None and retained is not None and retained != original:
            raise RootActionError('Retained original swap identity changed.', 'EXTERNAL_EDIT')
        if current not in (None, original, candidate):
            raise RootActionError('Selected swap identity changed during rollback.', 'EXTERNAL_EDIT')
        # Stop a replacement before unlink/rename; if swapoff fails, both the
        # active candidate and the retained original remain exactly in place.
        if current is not None and current != original:
            self._set_active(path, False)
            self._remove_inactive(path, candidate)
            current = None
        if original is not None and current is None:
            if retained is None:
                raise RootActionError('The retained original swap is missing.', 'RECOVERY_REQUIRED')
            self._move(retained_path, path)
        self._root._restore_files(journal)
        self._checked(['systemctl', 'daemon-reload'])
        if original is not None:
            self._set_active(path, journal['before']['active'])
        elif path in self._swaps():
            raise RootActionError('Unexpected active swap prevents recovery.', 'RECOVERY_REQUIRED')
        staging = journal['candidate_path']
        if self.host.path(staging).exists():
            if staging in self._swaps():
                raise RootActionError('An active staged swap prevents recovery.', 'RECOVERY_REQUIRED')
            if candidate is not None:
                self._remove_inactive(staging, candidate)
            else:
                target = self.host.path(staging)
                info = target.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise RootActionError('Incomplete staged inode is unsafe.', 'EXTERNAL_EDIT')
                target.unlink()
        if self._identity(path, original) != original or (path in self._swaps()) != journal['before']['active']:
            raise RootActionError('Swap rollback verification failed.', 'RECOVERY_REQUIRED')

    def _result(self, journal, status, message, **extra):
        result = {'module': 'swap', 'status': status, 'transaction_id': journal['transaction_id'], 'message': message, **extra}
        return self._root._log_result(result)

    def apply(self, plan):
        self._gate()
        if not isinstance(plan, dict) or plan.get('fingerprint') != _fingerprint(plan):
            raise RootActionError('Swap preview was modified.', 'STALE_PLAN')
        config = normalize(plan.get('config'))
        with self._root._lock():
            self._root._check_pending()
            live = self.plan(config)
            # Usage/free memory may change between preview and execution. The
            # selected inode, fstab, private state and requested work may not.
            for key in ('schema', 'module', 'config', 'state_fingerprint', 'replace_file', 'fstab_before_sha256', 'fstab_after_sha256', 'fstab_entry', 'fstab_action', 'notes'):
                if live.get(key) != plan.get(key):
                    raise RootActionError('Swap state changed after preview.', 'STALE_PLAN')
            before = live['observations']
            unchanged = (not live['replace_file'] and before['active'] == config['active']
                         and before['persistent'] == config['persistent']
                         and live['fstab_before_sha256'] == live['fstab_after_sha256']
                         and self.load_saved() == config)
            if unchanged:
                return {'status': 'NO_CHANGE', 'module': 'swap', 'message': 'Selected swap already matches the saved configuration.', 'verification': self._verify(config)}
            identifier = self._root._id()
            directory = f'{STAGING}/{identifier}'
            self._root._secure_dir(directory)
            if self.host.path(directory).stat().st_dev != self.host.path('/').stat().st_dev:
                raise RootActionError('Swap staging must be on the target root filesystem.', 'SWAP_UNSUPPORTED')
            self._root._secure_dir('/opt/3waServerSetting/config/swap')
            _, _, after_fstab = self._fstab(config['path'], before['identity'], config['persistent'])
            journal = {'transaction_id': identifier, 'kind': 'swap', 'module': 'swap', 'status': 'SWAP_PREPARED',
                       'config': config, 'before': before, 'replace_file': live['replace_file'],
                       'original_path': directory + '/original.swap', 'candidate_path': directory + '/candidate.swap',
                       'candidate_identity': None, 'original_disposed': False}
            self._backup(journal, after_fstab)
            self._root._journal(journal)
            try:
                if live['replace_file']:
                    if self.host.free_bytes() < config['size_mib'] * MIB + RESERVE_BYTES:
                        raise RootActionError('Available disk space changed before allocation.', 'SWAP_DISK_SPACE')
                    self._zero_fill(journal['candidate_path'], config['size_mib'])
                    journal['candidate_identity'] = self._identity(journal['candidate_path'])
                    journal['status'] = 'SWAP_CANDIDATE_READY'
                    self._root._journal(journal)
                    self._set_active(config['path'], False)
                    if before['identity'] is not None:
                        self._move(config['path'], journal['original_path'])
                    self._move(journal['candidate_path'], config['path'])
                    journal['status'] = 'SWAP_REPLACED'
                    self._root._journal(journal)
                owned = self._state_json(OWNED, {})
                owned[config['path']] = self._identity(config['path'])
                owned_bytes = (json.dumps(owned, sort_keys=True, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
                next(entry for entry in journal['files'] if entry['path'] == OWNED)['after_sha256'] = _digest(owned_bytes)
                self._root._journal(journal)
                self._root._private_json(OWNED, owned)
                self._set_active(config['path'], config['active'])
                self._root._atomic('/etc/fstab', after_fstab, journal['fstab_metadata'])
                if live['fstab_before_sha256'] != live['fstab_after_sha256']:
                    self._checked(['systemctl', 'daemon-reload'])
                self._root._private_json(SAVED, config)
                verification = self._verify(config)
                journal['after_identity'] = self._identity(config['path'])
                journal['owned_after_sha256'] = _digest(self._bytes(OWNED))
                # Once cleanup starts, the durable record commits the verified
                # configuration. A crash here must finish cleanup, not promise
                # recovery of paging data that may already have been deleted.
                journal['status'] = 'SWAP_VERIFIED_CLEANUP'
                self._root._journal(journal)
                if self.host.path(journal['original_path']).exists():
                    self._remove_inactive(journal['original_path'], before['identity'])
                journal['original_disposed'] = before['identity'] is not None and live['replace_file']
                journal['status'] = 'SWAP_APPLIED'
                self._root._journal(journal)
                return self._result(journal, 'SWAP_APPLIED', 'Swap configuration verified; inactive original paging data was removed.', verification=verification)
            except Exception as error:
                if journal['status'] == 'SWAP_VERIFIED_CLEANUP':
                    journal['status'] = 'SWAP_CLEANUP_REQUIRED'
                    self._root._journal(journal)
                    return self._result(journal, 'RECOVERY_REQUIRED', 'The new swap is verified; recover this transaction to finish inactive original cleanup.')
                journal['status'] = 'SWAP_ROLLING_BACK'
                journal['failure_code'] = error.code if isinstance(error, RootActionError) else 'SWAP_IO_FAILED'
                self._root._journal(journal)
                try:
                    self._rollback(journal)
                    journal['status'] = 'SWAP_ROLLED_BACK'
                    self._root._journal(journal)
                    return self._result(journal, 'SWAP_ROLLED_BACK', 'Swap apply failed; the original file, activation and fstab were restored.')
                except Exception:
                    journal['status'] = 'SWAP_RECOVERY_REQUIRED'
                    self._root._journal(journal)
                    return self._result(journal, 'RECOVERY_REQUIRED', 'Swap recovery is incomplete; active files and the retained original were kept. Recover this transaction after inspection.')

    def recover(self, identifier):
        self._gate()
        if not isinstance(identifier, str) or not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}', identifier):
            raise RootActionError('Invalid swap transaction id.', 'UNSAFE_STATE')
        with self._root._lock():
            journals = self._root._journals()
            self._root._check_pending(ignore_id=identifier)
            selected = next((item for item in journals if item['transaction_id'] == identifier and item.get('kind') == 'swap'), None)
            if selected is None:
                raise RootActionError('Swap transaction not found.', 'NO_BACKUP')
            if selected['status'] == 'SWAP_ROLLED_BACK':
                return {'module': 'swap', 'status': 'SWAP_ROLLED_BACK', 'transaction_id': identifier,
                        'message': 'This swap transaction has already been recovered.'}
            later = [item for item in journals if item.get('kind') == 'swap' and item.get('created_ns', 0) > selected.get('created_ns', 0)
                     and item.get('status') != 'SWAP_ROLLED_BACK']
            if later:
                raise RootActionError('A later swap transaction prevents recovery of this transaction.', 'EXTERNAL_EDIT')
            if selected['status'] in {'SWAP_CLEANUP_REQUIRED', 'SWAP_VERIFIED_CLEANUP'}:
                self._verify(selected['config'])
                self._remove_inactive(selected['original_path'], selected['before']['identity'])
                selected['original_disposed'] = True
                selected['status'] = 'SWAP_APPLIED'
                self._root._journal(selected)
                return self._result(selected, 'SWAP_APPLIED', 'Verified swap retained; inactive original cleanup completed.')
            if selected['status'] == 'SWAP_APPLIED' and selected['before']['identity'] is not None and selected['replace_file']:
                raise RootActionError('Successful resize removed the old paging data; use a new preview to rebuild the previous capacity.', 'NO_BACKUP')
            if selected['status'] == 'SWAP_APPLIED':
                if self._identity(selected['config']['path']) != selected.get('after_identity'):
                    raise RootActionError('Selected swap changed after apply.', 'EXTERNAL_EDIT')
                if _digest(self._bytes(OWNED)) != selected.get('owned_after_sha256'):
                    raise RootActionError('Swap ownership state changed after apply.', 'EXTERNAL_EDIT')
            try:
                self._rollback(selected)
                selected['status'] = 'SWAP_ROLLED_BACK'
                self._root._journal(selected)
                return self._result(selected, 'SWAP_ROLLED_BACK', 'Original swap configuration restored; paging data is not a configuration backup.')
            except Exception:
                selected['status'] = 'SWAP_RECOVERY_REQUIRED'
                self._root._journal(selected)
                return self._result(selected, 'RECOVERY_REQUIRED', 'Swap recovery could not complete; active files were preserved.')

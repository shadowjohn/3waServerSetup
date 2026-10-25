"""Owned boot configuration; restoring configuration never runs or stops a job."""
import base64
import json
import os
from pathlib import Path
import posixpath
import re
import stat
import subprocess
import tempfile
import time

from .root_actions import (BACKUPS, FINAL_STATUSES, Host, RootActionError, RootManager,
                           TRUSTED_PATH, _digest, _fingerprint, _json)
from .trusted_commands import environment, resolve


DIRECTORY = '/opt/3waServerSetting/config/startup'
SCRIPT = DIRECTORY + '/startup.sh'
SAVED = DIRECTORY + '/draft.json'
STATE = '/var/lib/3wa-server-setup/startup'
OWNED = STATE + '/owned.json'
UNIT_NAME = 'threewa-startup.service'
UNIT = '/etc/systemd/system/' + UNIT_NAME
BOOT_LINK = '/etc/systemd/system/multi-user.target.wants/' + UNIT_NAME
PATHS = (SCRIPT, UNIT, OWNED)
LIMIT = 65536
EXEC = ('/usr/bin/env -i PATH=' + TRUSTED_PATH + ' HOME=/root LANG=C LC_ALL=C '
        '/bin/bash --noprofile --norc ' + SCRIPT)


def default_config():
    return {'schema': 1, 'script': '#!/bin/bash\nset -e\n\n# 在這裡加入開機指令；儲存與套用設定不會立即執行。\n',
            'enabled': False, 'wait_network': True, 'timeout_seconds': 120}


def normalize(config):
    if (not isinstance(config, dict) or set(config) != set(default_config())
            or type(config.get('schema')) is not int or config['schema'] != 1
            or not isinstance(config.get('script'), str)
            or type(config.get('enabled')) is not bool or type(config.get('wait_network')) is not bool
            or type(config.get('timeout_seconds')) is not int or not 1 <= config['timeout_seconds'] <= 600):
        raise RootActionError('A complete schema 1 Startup configuration is required.', 'STARTUP_INVALID')
    script = config['script'].replace('\r\n', '\n')
    try:
        valid = not script.startswith('\ufeff') and '\x00' not in script and len(script.encode('utf-8')) <= LIMIT
    except UnicodeError:
        valid = False
    if not valid:
        raise RootActionError('Startup script must be UTF-8 without BOM/NUL and at most 65536 bytes.', 'STARTUP_INVALID')
    return dict(config, script=script)


def _unit(config):
    network = 'Wants=network-online.target\nAfter=network-online.target\n' if config['wait_network'] else ''
    return ('# Managed by 3waServerSetup Startup schema1\n[Unit]\nDescription=3WA user startup script\n'
            + network + '\n[Service]\nType=oneshot\nRemainAfterExit=yes\nRestart=no\n'
            'User=root\nGroup=root\nWorkingDirectory=/root\n'
            'UMask=0077\nUnsetEnvironment=LD_PRELOAD LD_LIBRARY_PATH BASH_ENV ENV PYTHONPATH PYTHONHOME PHPRC PHP_INI_SCAN_DIR LD_AUDIT LD_DEBUG LD_DEBUG_OUTPUT GCONV_PATH\n'
            'ExecStart=' + EXEC + '\nTimeoutStartSec=' + str(config['timeout_seconds']) + '\n'
            'TimeoutStopSec=5s\nKillMode=control-group\n'
            'StandardOutput=journal\nStandardError=journal\n\n[Install]\nWantedBy=multi-user.target\n').encode()


def _links(host):
    links = []
    for directory in ('/etc/systemd/system', '/run/systemd/system', '/usr/lib/systemd/system'):
        root = host.path(directory)
        if not root.exists():
            continue
        for current, directories, files in os.walk(root, followlinks=False):
            for name in directories + files:
                path = Path(current) / name
                if not path.is_symlink():
                    continue
                target = path.readlink().as_posix()
                virtual = '/' + path.relative_to(host.root).as_posix()
                normalized = posixpath.normpath(target if target.startswith('/') else posixpath.join(posixpath.dirname(virtual), target))
                if name == UNIT_NAME or posixpath.basename(normalized) == UNIT_NAME:
                    links.append({'path': virtual, 'target': normalized, 'uid': path.lstat().st_uid})
    return sorted(links, key=lambda entry: entry['path'])


class StartupHost(Host):
    def run(self, argv, timeout=30):
        if (not isinstance(argv, (list, tuple)) or not argv or argv[0] not in {'bash', 'systemctl', 'systemd-analyze'}
                or any(not isinstance(arg, str) or '\x00' in arg for arg in argv)
                or not isinstance(timeout, (int, float)) or not 0 < timeout <= 900):
            raise RootActionError('Invalid bounded Startup command.', 'UNSAFE_COMMAND')
        if self.root != Path('/').absolute() or getattr(os, 'geteuid', lambda: -1)() != 0:
            raise RootActionError('Native Startup commands require the privileged local host.', 'ROOT_REQUIRED')
        executable = resolve(argv[0])
        if executable is None:
            raise RootActionError('A trusted Startup command is unavailable.', 'MISSING_COMMAND')
        try:
            result = subprocess.run([executable, *argv[1:]], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors='replace',
                timeout=timeout, check=False, shell=False, env=environment())
        except subprocess.TimeoutExpired:
            raise RootActionError('Startup command timed out; inspect the native unit result.', 'TIMEOUT') from None
        except OSError:
            raise RootActionError('Startup command could not execute.', 'STARTUP_COMMAND_FAILED') from None
        if len(result.stdout) + len(result.stderr) > 1024 * 1024:
            raise RootActionError('Startup command output exceeded its bound.', 'STARTUP_COMMAND_FAILED')
        return result

    def startup_links(self):
        return _links(self)


class StartupManager:
    def __init__(self, host=None):
        self.host = host or StartupHost()
        self._root = RootManager(self.host)

    def _gate(self):
        self._root._gate('time')

    def _bytes(self, absolute, limit=1024 * 1024):
        path = self.host.path(absolute)
        if not path.exists():
            return None
        self._root._metadata(path)
        with path.open('rb') as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise RootActionError('Startup state exceeds its bound.', 'STARTUP_UNSUPPORTED')
        return data

    def _state(self):
        data = self._bytes(OWNED)
        if data is None:
            return None
        self._root._trusted_file(self.host.path(OWNED))
        try:
            value = json.loads(data)
            if value['schema'] != 1 or type(value['present']) is not bool or not isinstance(value['files'], dict):
                raise ValueError()
            normalize(dict(value['config'], script=''))
            return value
        except (ValueError, KeyError, TypeError, RootActionError):
            raise RootActionError('Startup ownership state is invalid.', 'UNSAFE_STATE') from None

    def _stamp(self, absolute):
        data = self._bytes(absolute)
        if data is None:
            return None
        path = self.host.path(absolute); info = path.stat()
        return {'sha256': _digest(data), 'metadata': self._root._metadata(path),
                'inode': info.st_ino, 'device': info.st_dev}

    def _content_stamp(self, value):
        return {key: value[key] for key in ('sha256', 'metadata')} if value is not None else None

    def _metadata_for(self, mode):
        # Windows prefixes exercise bytes/rollback; native writes remain Linux-only.
        return {'mode': mode if os.name == 'posix' else 0o666, 'uid': 0, 'gid': 0, 'xattrs': {}}

    def _runtime(self, allow_transition=False):
        properties = ('LoadState', 'ActiveState', 'SubState', 'UnitFileState', 'FragmentPath', 'DropInPaths',
            'Transient', 'Result', 'ExecMainStatus', 'ExecMainCode', 'InvocationID',
            'ExecMainStartTimestampMonotonic', 'ExecMainExitTimestampMonotonic', 'ExecStart')
        result = self.host.run(['systemctl', 'show', UNIT_NAME, *['--property=' + key for key in properties]], timeout=8)
        values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if result.returncode or any(key not in values for key in ('LoadState', 'ActiveState', 'SubState')):
            raise RootActionError('Cannot inspect Startup systemd state.', 'STARTUP_UNSUPPORTED')
        if (values.get('DropInPaths') or values.get('Transient', 'no') != 'no'
                or values.get('FragmentPath', '') not in {'', UNIT}
                or values['LoadState'] not in {'loaded', 'not-found'}
                or values.get('UnitFileState', '') not in {'', 'enabled', 'disabled'}):
            raise RootActionError('Unknown Startup unit, drop-in or enablement layout.', 'STARTUP_UNSUPPORTED')
        transition = values['ActiveState'] in {'activating', 'deactivating', 'reloading'}
        if transition and not allow_transition:
            raise RootActionError('Startup is changing state; wait for its native result.', 'BUSY')
        if not transition and values['ActiveState'] not in {'inactive', 'active', 'failed'}:
            raise RootActionError('Unknown Startup runtime state.', 'STARTUP_UNSUPPORTED')
        if values['LoadState'] == 'loaded':
            command = values.get('ExecStart', '')
            if 'path=/usr/bin/env ;' not in command or 'argv[]=' + EXEC + ' ;' not in command:
                raise RootActionError('Unknown loaded Startup command.', 'STARTUP_UNSUPPORTED')
        links = self.host.startup_links() if hasattr(self.host, 'startup_links') else _links(self.host)
        if any(link['path'] != BOOT_LINK or link['target'] != UNIT or link['uid'] != 0 for link in links):
            raise RootActionError('Unknown Startup wants/requires/alias link; no enablement was changed.', 'STARTUP_UNSUPPORTED')
        for link in links:
            parent = self.host.path(posixpath.dirname(link['path']))
            if parent.exists():
                self._root._trusted_target(parent / '.3wa-parent-trust-check')
        if bool(links) != (values.get('UnitFileState') == 'enabled'):
            raise RootActionError('Startup enablement does not match its owned boot link.', 'STARTUP_UNSUPPORTED')
        return {'load_state': values['LoadState'], 'active_state': values['ActiveState'], 'sub_state': values['SubState'],
                'enabled': bool(links), 'result': values.get('Result', ''), 'exec_main_status': values.get('ExecMainStatus', ''),
                'exec_main_code': values.get('ExecMainCode', ''), 'invocation_id': values.get('InvocationID', ''),
                'start_us': values.get('ExecMainStartTimestampMonotonic', '0'),
                'exit_us': values.get('ExecMainExitTimestampMonotonic', '0')}

    def _pending(self):
        pending = [journal for journal in self._root._journals() if journal.get('module') == 'startup'
                   and journal.get('status') not in FINAL_STATUSES]
        if len(pending) > 1:
            raise RootActionError('Multiple Startup transactions require inspection.', 'RECOVERY_REQUIRED')
        return pending[0] if pending else None

    def _observe(self, allow_transition=False):
        files = {path: self._stamp(path) for path in PATHS}
        owned = self._state(); runtime = self._runtime(allow_transition)
        if owned is None:
            if files[SCRIPT] is not None or files[UNIT] is not None or runtime['load_state'] == 'loaded':
                raise RootActionError('An unowned Startup file or loaded unit exists.', 'STARTUP_UNSUPPORTED')
        elif owned['present']:
            if set(owned['files']) != {SCRIPT, UNIT} or any(
                    self._content_stamp(files[path]) != owned['files'][path] for path in (SCRIPT, UNIT)):
                raise RootActionError('Startup files were modified outside this tool.', 'EXTERNAL_EDIT')
            flags = owned.get('config', {})
            if files[UNIT]['sha256'] != _digest(_unit(flags)):
                raise RootActionError('Startup unit does not match its owned configuration.', 'UNSAFE_STATE')
        elif files[SCRIPT] is not None or files[UNIT] is not None or runtime['enabled']:
            raise RootActionError('Files or enablement conflict with the Startup tombstone.', 'EXTERNAL_EDIT')
        elif owned.get('last_unit_sha256') != _digest(_unit(owned['config'])):
            raise RootActionError('Startup tombstone identity is invalid.', 'UNSAFE_STATE')
        return {'files': files, 'runtime': runtime}

    def status(self):
        pending = None
        try:
            self._gate(); pending = self._pending()
            observed = self._recovery_view(pending, allow_transition=True) if pending and pending.get('kind') != 'startup-run' else self._observe(allow_transition=True)
            owned = self._state(); flags = owned.get('config', {}) if owned and owned['present'] else {}
            native = self.host.root / 'etc/rc.local'
            rc = {'exists': native.exists() or native.is_symlink(),
                  'executable': bool(native.exists() and native.stat().st_mode & 0o111),
                  'service_active_state': 'unknown'}
            result = self.host.run(['systemctl', 'show', 'rc-local.service', '--property=ActiveState'], timeout=5)
            if not result.returncode:
                rc['service_active_state'] = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line).get('ActiveState', 'unknown')
            return {'module': 'startup', 'supported': True, 'detail': 'Configuration and boot enablement only; script side effects are not reversible.',
                    'managed': bool(owned and owned['present']), 'enabled': observed['runtime']['enabled'],
                    'wait_network': flags.get('wait_network'), 'timeout_seconds': flags.get('timeout_seconds'),
                    'script_sha256': (observed['files'][SCRIPT] or {}).get('sha256'), 'unit': UNIT_NAME,
                    'runtime': observed['runtime'], 'rc_local': rc,
                    'pending': {key: pending[key] for key in ('transaction_id', 'status', 'kind')} if pending else None}
        except (RootActionError, OSError):
            return {'module': 'startup', 'supported': False, 'managed': False,
                    'detail': 'Startup state is unsupported or needs recovery; no script was executed.',
                    'pending': {key: pending[key] for key in ('transaction_id', 'status', 'kind')} if pending else None}

    def load_current(self):
        self._gate(); observed = self._observe(); owned = self._state()
        if owned is None or not owned['present']:
            return None
        try:
            return normalize(dict(owned['config'], enabled=observed['runtime']['enabled'],
                                  script=self._bytes(SCRIPT, LIMIT).decode('utf-8')))
        except UnicodeError:
            raise RootActionError('Managed Startup script is not UTF-8.', 'UNSAFE_STATE') from None

    def load_saved(self):
        self._gate(); data = self._bytes(SAVED)
        if data is None:
            return None
        self._root._trusted_file(self.host.path(SAVED))
        try:
            return normalize(json.loads(data))
        except (ValueError, UnicodeError):
            raise RootActionError('Saved Startup draft is invalid.', 'STARTUP_INVALID') from None

    def save(self, config):
        config = normalize(config); self._gate()
        with self._root._lock():
            self._root._check_pending()
            self._root._secure_dir(DIRECTORY)
            self._root._private_json(SAVED, config)
        return {'status': 'SAVED', 'module': 'startup', 'message': 'Private Startup draft saved; nothing was executed.'}

    def plan(self, config):
        config = normalize(config); self._gate()
        plan = {'schema': 1, 'module': 'startup', 'operation': 'apply', 'config': config, 'before': self._observe(),
                'notes': ['Apply changes the script, unit and boot enablement only; it never starts, restarts or stops a job.',
                          'Network-online ordering is not an Internet connectivity check.',
                          'Script output goes to the native journal; business side effects cannot be restored.']}
        plan['fingerprint'] = _fingerprint(plan)
        return plan

    def _checked(self, argv, timeout=30):
        result = self.host.run(argv, timeout)
        if result.returncode:
            raise RootActionError('A checked Startup command failed; native output was not copied.', 'STARTUP_COMMAND_FAILED')
        return result

    def _validate(self, config):
        self._root._secure_dir(STATE)
        with tempfile.TemporaryDirectory(prefix='verify-', dir=self.host.path(STATE)) as directory:
            script = Path(directory) / 'startup.sh'; script.write_bytes(config['script'].encode()); script.chmod(0o700)
            unit = Path(directory) / UNIT_NAME; unit.write_bytes(_unit(config)); unit.chmod(0o600)
            self._checked(['bash', '--noprofile', '--norc', '-n', str(script)], 8)
            self._checked(['systemd-analyze', '--man=no', 'verify', str(unit)], 15)

    def _snapshot(self):
        return {'files': {path: {'content': base64.b64encode(data).decode() if data is not None else None,
                    'metadata': self._root._metadata(self.host.path(path))} for path in PATHS
                    for data in [self._bytes(path)]}, 'enabled': self._runtime()['enabled']}

    def _backup(self, journal, before):
        absolute = f"{BACKUPS}/{journal['transaction_id']}/startup.json"
        self._root._secure_dir(f"{BACKUPS}/{journal['transaction_id']}")
        self._root._private_json(absolute, before)
        journal['backup_sha256'] = _digest(self._bytes(absolute))

    def _read_backup(self, journal):
        absolute = f"{BACKUPS}/{journal['transaction_id']}/startup.json"
        self._root._trusted_file(self.host.path(absolute)); data = self._bytes(absolute)
        if _digest(data) != journal['backup_sha256']:
            raise RootActionError('Startup backup integrity check failed.', 'RECOVERY_REQUIRED')
        try:
            return json.loads(data)
        except (TypeError, ValueError):
            raise RootActionError('Startup backup is invalid.', 'RECOVERY_REQUIRED') from None

    def _expected(self, snapshot):
        return {path: {'sha256': _digest(base64.b64decode(entry['content'])), 'metadata': entry['metadata']}
                if entry['content'] is not None else None for path, entry in snapshot['files'].items()}

    def _recovery_view(self, journal, allow_transition=False):
        files = {path: self._stamp(path) for path in PATHS}
        for path in PATHS:
            current = self._content_stamp(files[path])
            allowed = [journal['before_files'][path], journal['after_files'][path]]
            if path == OWNED and journal['before_files'][OWNED] is None:
                tombstone = (_json({'schema': 1, 'present': False, 'files': {},
                    'last_unit_sha256': journal['unit_sha256'], 'config': journal['config']}) + '\n').encode()
                allowed.append({'sha256': _digest(tombstone), 'metadata': self._metadata_for(0o600)})
            if current not in allowed:
                raise RootActionError('An external Startup edit prevents recovery.', 'EXTERNAL_EDIT')
        return {'files': files, 'runtime': self._runtime(allow_transition)}

    def _write_snapshot(self, snapshot, tombstone=None):
        # 不呼叫 generic service restore，避免補償時重跑或停止使用者工作。
        if not snapshot['enabled'] and self._runtime()['enabled']:
            self._checked(['systemctl', 'disable', UNIT_NAME], 8)
        for path in PATHS:
            entry = snapshot['files'][path]
            if entry['content'] is None:
                target = self.host.path(path)
                if target.exists():
                    target.unlink()
                    if os.name == 'posix':
                        descriptor = os.open(target.parent, os.O_DIRECTORY)
                        try: os.fsync(descriptor)
                        finally: os.close(descriptor)
            else:
                self._root._atomic(path, base64.b64decode(entry['content']), entry['metadata'])
        if snapshot['files'][OWNED]['content'] is None and tombstone is not None:
            self._root._private_json(OWNED, {'schema': 1, 'present': False, 'files': {},
                'last_unit_sha256': tombstone.get('unit_sha256'), 'config': tombstone.get('config', {})})
        self._checked(['systemctl', 'daemon-reload'], 8)
        runtime = self._runtime()
        if runtime['enabled'] != snapshot['enabled']:
            self._checked(['systemctl', 'enable' if snapshot['enabled'] else 'disable', UNIT_NAME], 8)

    def _finish(self, journal, status, message, **extra):
        journal['status'] = status; self._root._journal(journal)
        return self._root._log_result({'module': 'startup', 'status': status,
            'transaction_id': journal['transaction_id'], 'message': message, **extra})

    def _check_preview(self, preview, operation):
        if (not isinstance(preview, dict) or preview.get('module') != 'startup'
                or preview.get('operation') != operation or preview.get('fingerprint') != _fingerprint(preview)):
            raise RootActionError('Startup preview was modified; review again.', 'STALE_PLAN')

    def apply(self, preview):
        self._gate(); self._check_preview(preview, 'apply')
        with self._root._lock():
            self._root._check_pending()
            current = self.plan(preview.get('config'))
            if current['fingerprint'] != preview['fingerprint']:
                raise RootActionError('Startup state changed after preview.', 'STALE_PLAN')
            config = current['config']
            if self.load_current() == config and current['before']['runtime']['enabled'] == config['enabled']:
                return {'module': 'startup', 'status': 'NO_CHANGE', 'message': 'Startup configuration already matches; nothing was run or reloaded.'}
            self._root._preflight_state(); self._root._secure_dir(DIRECTORY); self._root._secure_dir(STATE)
            self._validate(config)
            before = self._snapshot(); identifier = self._root._id()
            metadata = {path: self._metadata_for(mode) for path, mode in ((SCRIPT, 0o700), (UNIT, 0o644), (OWNED, 0o600))}
            contents = {SCRIPT: config['script'].encode(), UNIT: _unit(config)}
            flags = {key: value for key, value in config.items() if key != 'script'}
            owned = {'schema': 1, 'present': True, 'config': flags, 'files': {path:
                     {'sha256': _digest(contents[path]), 'metadata': metadata[path]} for path in (SCRIPT, UNIT)}}
            contents[OWNED] = _json(owned).encode()
            journal = {'schema': 1, 'module': 'startup', 'kind': 'startup-config', 'transaction_id': identifier,
                'status': 'STARTUP_PREPARED', 'before_files': self._expected(before),
                'after_files': {path: {'sha256': _digest(contents[path]), 'metadata': metadata[path]} for path in PATHS},
                'unit_sha256': _digest(contents[UNIT]), 'config': flags}
            self._backup(journal, before); self._root._journal(journal)
            try:
                journal['status'] = 'STARTUP_APPLYING'; self._root._journal(journal)
                self._recovery_view(journal)
                for path in PATHS:
                    self._root._atomic(path, contents[path], metadata[path])
                self._checked(['systemctl', 'daemon-reload'], 8)
                if before['enabled'] != config['enabled']:
                    self._checked(['systemctl', 'enable' if config['enabled'] else 'disable', UNIT_NAME], 8)
                after = self._observe()
                if after['runtime']['enabled'] != config['enabled']:
                    raise RootActionError('Startup boot enablement was not verified.', 'STARTUP_COMMAND_FAILED')
                journal['after'] = after
                return self._finish(journal, 'STARTUP_APPLIED', 'Startup configuration and boot enablement applied; script was not executed.')
            except Exception:
                try:
                    self._recovery_view(journal); self._write_snapshot(before, journal)
                    self._finish(journal, 'STARTUP_ROLLED_BACK', 'Startup configuration restored after failure; runtime was preserved.')
                except Exception:
                    journal['status'] = 'RECOVERY_REQUIRED'; self._root._journal(journal)
                    raise RootActionError('Startup configuration needs recovery; inspect its private backup.', 'RECOVERY_REQUIRED') from None
                raise RootActionError('Startup apply failed; configuration was restored without running or stopping a job.', 'STARTUP_ROLLED_BACK') from None

    def run_plan(self):
        self._gate(); observed = self._observe(); owned = self._state()
        if owned is None or not owned['present']:
            raise RootActionError('Apply an owned Startup configuration before running it.', 'STARTUP_NOT_READY')
        preview = {'schema': 1, 'module': 'startup', 'operation': 'run', 'unit': UNIT_NAME,
            'script_sha256': observed['files'][SCRIPT]['sha256'], 'timeout_seconds': owned['config']['timeout_seconds'],
            'before': observed, 'notes': ['Run executes root Bash code; its business side effects cannot be restored.',
                'An already active unit is not run again. Output remains in the native journal.']}
        preview['fingerprint'] = _fingerprint(preview)
        return preview

    def _boot_id(self):
        value = (self.host.read('/proc/sys/kernel/random/boot_id') or '').strip()
        if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', value):
            raise RootActionError('Native boot identity is unavailable.', 'STARTUP_UNSUPPORTED')
        return value

    def _run_result(self, journal):
        current = self._observe(); runtime = current['runtime']
        if current['files'][SCRIPT]['sha256'] != journal['script_sha256']:
            raise RootActionError('Startup script identity changed during execution.', 'RECOVERY_REQUIRED')
        if self._boot_id() != journal['boot_id']:
            raise RootActionError('Startup run crossed a reboot; its completion is unknown.', 'RECOVERY_REQUIRED')
        try:
            started = int(runtime['start_us']); exited = int(runtime['exit_us'])
            valid = (re.fullmatch(r'[0-9a-f]{32}', runtime['invocation_id'])
                and runtime['invocation_id'] != journal['previous_invocation']
                and started >= journal['intent_us'] and exited >= started
                and runtime['exec_main_code'] in {'1', '2', '3'})
        except ValueError:
            valid = False
        if not valid:
            raise RootActionError('Startup run result cannot be attributed to this request; it will not be retried.', 'RECOVERY_REQUIRED')
        success = (runtime['result'] == 'success' and runtime['exec_main_code'] == '1'
                   and runtime['exec_main_status'] == '0' and runtime['active_state'] == 'active'
                   and runtime['sub_state'] == 'exited')
        return self._finish(journal, 'STARTUP_RUN_SUCCEEDED' if success else 'STARTUP_RUN_FAILED',
            'Startup native execution completed; business side effects were not rolled back.', verification=runtime)

    def run_once(self, preview):
        self._gate(); self._check_preview(preview, 'run')
        with self._root._lock():
            self._root._check_pending()
            current = self.run_plan()
            if current['fingerprint'] != preview['fingerprint']:
                raise RootActionError('Startup run preview is stale.', 'STALE_PLAN')
            if current['before']['runtime']['active_state'] == 'active':
                return {'module': 'startup', 'status': 'NO_CHANGE', 'message': 'Startup is already active; it was not run again.'}
            self._root._preflight_state()
            journal = {'schema': 1, 'module': 'startup', 'kind': 'startup-run', 'transaction_id': self._root._id(),
                'status': 'STARTUP_RUNNING', 'script_sha256': current['script_sha256'],
                'boot_id': self._boot_id(), 'intent_us': time.monotonic_ns() // 1000,
                'previous_invocation': current['before']['runtime']['invocation_id']}
            self._root._journal(journal)
            try:
                self._checked(['systemctl', 'start', UNIT_NAME], current['timeout_seconds'] + 15)
            except Exception:
                pass
            return self._run_result(journal)

    def _restore_candidate(self):
        pending = self._pending()
        if pending is not None:
            return pending, True
        journals = self._root._journals()
        consumed = {journal.get('source_transaction_id') for journal in journals
                    if journal.get('module') == 'startup' and journal.get('kind') == 'startup-restore'
                    and journal.get('status') == 'STARTUP_RESTORED'}
        candidates = [journal for journal in journals if journal.get('module') == 'startup'
                      and journal.get('kind') == 'startup-config' and journal.get('status') == 'STARTUP_APPLIED'
                      and journal['transaction_id'] not in consumed]
        if not candidates:
            raise RootActionError('There is no Startup configuration to restore.', 'NO_BACKUP')
        return candidates[-1], False

    def restore_plan(self):
        self._gate(); journal, recovery = self._restore_candidate()
        before = self._observe() if journal['kind'] == 'startup-run' or not recovery else self._recovery_view(journal)
        if not recovery and before != journal['after']:
            # 執行狀態本來就會變化；配置 restore 只守住檔案與 boot enablement。
            if (any(self._content_stamp(before['files'][path]) != self._content_stamp(journal['after']['files'][path]) for path in PATHS)
                    or before['runtime']['enabled'] != journal['after']['runtime']['enabled']):
                raise RootActionError('Startup configuration changed after apply.', 'EXTERNAL_EDIT')
        preview = {'schema': 1, 'module': 'startup', 'operation': 'restore', 'transaction_id': journal['transaction_id'],
            'recovery': recovery, 'kind': journal['kind'], 'journal_status': journal['status'], 'before': before,
            'notes': ['Restore only configuration and boot enablement; runtime and script business side effects are preserved.',
                      'An interrupted run is observed only and is never retried.']}
        preview['fingerprint'] = _fingerprint(preview)
        return preview

    def restore(self, preview):
        self._gate(); self._check_preview(preview, 'restore')
        with self._root._lock():
            source, recovery = self._restore_candidate()
            self._root._check_pending(ignore_id=source['transaction_id'] if recovery else None)
            current = self.restore_plan()
            if current['fingerprint'] != preview['fingerprint']:
                raise RootActionError('Startup restore preview is stale.', 'STALE_PLAN')
            if source['kind'] == 'startup-run':
                return self._run_result(source)
            target = self._read_backup(source)
            if recovery:
                self._recovery_view(source); self._write_snapshot(target, source)
                return self._finish(source, 'STARTUP_ROLLED_BACK', 'Interrupted Startup configuration restored; runtime was preserved.')
            before = self._snapshot()
            journal = {'schema': 1, 'module': 'startup', 'kind': 'startup-restore', 'status': 'STARTUP_RESTORING',
                'transaction_id': self._root._id(), 'source_transaction_id': source['transaction_id'],
                'before_files': self._expected(before), 'after_files': self._expected(target),
                'unit_sha256': source['unit_sha256'], 'config': source['config']}
            # 首次 absent restore 的 ownership tombstone 是本交易可辨識的目標。
            if target['files'][OWNED]['content'] is None:
                tombstone = (_json({'schema': 1, 'present': False, 'files': {},
                    'last_unit_sha256': source['unit_sha256'], 'config': source['config']}) + '\n').encode()
                journal['after_files'][OWNED] = {'sha256': _digest(tombstone),
                    'metadata': self._metadata_for(0o600)}
            self._backup(journal, before); self._root._journal(journal)
            try:
                self._write_snapshot(target, source)
                self._observe()
                return self._finish(journal, 'STARTUP_RESTORED', 'Previous Startup configuration restored; runtime was preserved.')
            except Exception:
                try:
                    # 只有 restore 的完成 journal 消耗來源；補償前先恢復 pending，
                    # 以涵蓋完成寫入已成功、回報失敗後又於補償中斷的情況。
                    journal['status'] = 'STARTUP_RESTORING'; self._root._journal(journal)
                    self._recovery_view(journal); self._write_snapshot(before, journal)
                    return self._finish(journal, 'STARTUP_ROLLED_BACK', 'Restore failed; prior Startup configuration was recovered without changing runtime.')
                except Exception:
                    journal['status'] = 'RECOVERY_REQUIRED'; self._root._journal(journal)
                    raise RootActionError('Startup restore requires private-backup inspection.', 'RECOVERY_REQUIRED') from None

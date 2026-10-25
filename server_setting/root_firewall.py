"""Guarded native nftables controller; confirmation belongs to a new SSH transport."""
import ipaddress
import json
from pathlib import Path
import re
import secrets
import time

from .root_actions import RootActionError, RootManager
from . import firewall_watchdog as guard
from .firewall_rules import normalize, normalize_rescue, render

DRAFT_DIR = '/opt/3waServerSetting/config/firewall'
DRAFT = DRAFT_DIR + '/draft.json'
GENERATION = re.compile(r'[0-9a-f]{24}')


class FirewallManager:
    def __init__(self, host=None):
        self.core = RootManager(host)
        self.host = self.core.host
        self.runtime = guard.Runtime(self.host.root, self.host.run)

    def _error(self, error):
        if isinstance(error, RootActionError):
            return error
        return RootActionError(str(error) if isinstance(error, (guard.FirewallError, ValueError)) else
                               'Firewall operation failed; inspect private transaction state.', 'FIREWALL_FAILED')

    def _gate(self):
        self.core._gate('time')

    def _pending(self):
        return [journal for journal in self.core._journals() if journal.get('kind') == 'firewall' and journal['status'] not in guard.FINAL]

    def _preflight(self):
        self._gate()
        for unit in ('nftables.service', 'firewalld.service', 'docker.service', 'containerd.service',
                     'kubelet.service', 'netfilter-persistent.service'):
            result = self.host.run(['systemctl', 'show', unit, '--property=LoadState', '--property=ActiveState', '--property=UnitFileState'], timeout=5)
            if result.returncode:
                raise guard.FirewallError('Firewall manager discovery failed.')
            properties = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
            if properties.get('LoadState') == 'loaded' and (properties.get('ActiveState') == 'active'
                    or properties.get('UnitFileState') in {'enabled', 'enabled-runtime'}):
                raise guard.FirewallError('Another firewall/container manager is active or enabled.')
        ufw = self.host.read('/etc/ufw/ufw.conf')
        if ufw is not None:
            values = re.findall(r'^\s*ENABLED\s*=\s*(\S+)\s*$', ufw, re.M)
            if values != ['no']:
                raise guard.FirewallError('UFW is enabled or its configuration is ambiguous.')
        current = self.runtime.snapshot()
        if current['foreign_hooks']:
            from .root_banip import verify_companion
            companion_hooks = {('ip','filter','INPUT','input'), ('ip','filter','FORWARD','forward'),
                               ('ip','filter','OUTPUT','output')}
            if (any(tuple(hook.get(key) for key in ('family','table','name','hook')) not in companion_hooks
                    for hook in current['foreign_hooks']) or not verify_companion(self.runtime)):
                raise guard.FirewallError('A foreign nftables base hook requires separate integration.')
        for absolute in ('/proc/sys/net/ipv4/ip_forward', '/proc/sys/net/ipv6/conf/all/forwarding'):
            if (self.host.read(absolute) or '').strip() != '0':
                raise guard.FirewallError('Forwarding/routing hosts require separate firewall integration.')
        for executable in ('iptables', 'ip6tables'):
            result = self.host.run([executable, '--version'], timeout=5)
            if result.returncode or 'nf_tables' not in result.stdout:
                raise guard.FirewallError('A native nf_tables iptables backend is required.')
        if current['exists']:
            committed = self.runtime.read_json(guard.CONFIRMED)
            pending = self._pending()
            expected = committed.get('owned') if committed else None
            ours = expected is not None and self.runtime.policy_matches(current, expected)
            ours |= any(current['comment'] == guard.OWNER + ' generation=' + item['generation']
                        and (not item.get('after') or self.runtime.expected(current, item['after'])) for item in pending)
            if not ours:
                raise guard.FirewallError('Same-name firewall table is not owned by this installation or was edited externally.')
        return current

    def status(self):
        try:
            current = self._preflight()
            pending = self._pending()
            active = pending[-1] if pending else None
            committed = self.runtime.read_json(guard.CONFIRMED)
            return {'supported': True, 'active': current['exists'], 'backend': 'nftables', 'detail': 'Owned inet INPUT policy only; NAT/FORWARD and other tables are unchanged.',
                    'owned_table': current['exists'], 'persistent': committed is not None,
                    'pending': ({'transaction_id': active['transaction_id'], 'status': active['status'],
                                 'deadline_boottime': active.get('deadline_boottime'),
                                 'remaining_seconds': max(0, int(active.get('deadline_boottime', guard.now()) - guard.now()))} if active else None),
                    'saved': self.runtime.path(DRAFT).exists(),
                    'initial_restore_available': bool(committed and not self.runtime.journal(committed['transaction_id'])['before']['exists'])}
        except Exception as error:
            return {'supported': False, 'active': None, 'backend': 'nftables', 'detail': str(self._error(error)),
                    'owned_table': None, 'persistent': False, 'pending': None, 'saved': False, 'initial_restore_available': False}

    def load_saved(self):
        try:
            self._gate()
            value = self.runtime.read_json(DRAFT)
            return normalize(value) if value is not None else None
        except Exception as error:
            raise self._error(error) from None

    def save(self, config):
        try:
            self._gate()
            config = normalize(config)
            with self.core._lock():
                self.core._check_pending()
                self.runtime.write_json(DRAFT, config)
            return {'status': 'SAVED', 'module': 'firewall', 'message': 'Private firewall draft saved; no kernel rules changed.'}
        except Exception as error:
            raise self._error(error) from None

    def _plan(self, config, generation=None):
        config = normalize(config)
        before = self._preflight()
        origin = self.runtime.proof()
        address = ipaddress.ip_address(origin['peer_ip'])
        rescue = normalize_rescue({'port': origin['server_port'], 'sources': [str(ipaddress.ip_network(str(address) + ('/32' if address.version == 4 else '/128')))]})
        generation = generation or secrets.token_hex(12)
        if not GENERATION.fullmatch(generation):
            raise guard.FirewallError('Invalid server firewall generation.')
        source = self._render(config, rescue, before['exists'], generation, before['handle'])
        result = {'schema': 1, 'module': 'firewall', 'config': config, 'generation': generation,
                  'rescue': rescue, 'origin': origin, 'before': before, 'script': source, 'timeout_seconds': 120,
                  'notes': ['Default-drop inet INPUT for IPv4 and IPv6, with displayed mandatory control/SSH/established exceptions.',
                            'A separate root systemd watchdog must acknowledge readiness before kernel changes.',
                            'Confirm from a new SSH TCP transport after apply; the current GUI/session cannot confirm.',
                            'Only confirmed policy persists at boot. No NAT/FORWARD or foreign table is flushed.']}
        result['fingerprint'] = guard.digest(guard.encoded(result))
        return result

    def plan(self, config):
        try:
            return self._plan(config)
        except Exception as error:
            raise self._error(error) from None

    def _render(self, config, rescue, exists, generation, handle=None):
        source = render(config, rescue, table_exists=exists)
        marker = 'comment "' + guard.OWNER + '"'
        if source.count(marker) != 1:
            raise guard.FirewallError('Compiler table ownership marker is ambiguous.')
        source = source.replace(marker, 'comment "' + guard.OWNER + ' generation=' + generation + '"', 1)
        if exists:
            if type(handle) is not int or handle <= 0:
                raise guard.FirewallError('Owned table handle is unavailable.')
            source = source.replace('delete table inet ' + guard.TABLE, f'delete table inet handle {handle}', 1)
        return source

    def _install(self):
        self.runtime.ensure_dir('/etc/systemd/system', private=False)
        common = ('\nUser=root\nGroup=root\nNoNewPrivileges=yes\nKillMode=control-group\n'
                  'TimeoutStopSec=2s\nSendSIGKILL=yes\n'
                  'UnsetEnvironment=LD_PRELOAD LD_LIBRARY_PATH PYTHONPATH PYTHONHOME BASH_ENV ENV\n')
        templates = {}
        for role in ('guard', 'apply', 'confirm', 'rollback'):
            service = ('[Unit]\nDescription=3WA guarded firewall ' + role + '\n'
                       + ('RefuseManualStop=yes\n' if role == 'guard' else '') + '\n[Service]\n'
                       + ('Type=notify\nNotifyAccess=main\nRestart=on-failure\nRestartSec=1s\nWatchdogSec=60s\n' if role == 'guard' else 'Type=oneshot\n')
                       + 'TimeoutStartSec=12s\nExecStart=/usr/bin/python3 -I ' + guard.TRANSACTIONS + '/%i/watchdog.py ' + role + ' %i\n' + common)
            templates[f'threewa-fw-{role}@.service'] = service
        templates[guard.BOOT_UNIT] = ('[Unit]\nDescription=3WA confirmed firewall and pending recovery\nDefaultDependencies=no\n'
                'After=local-fs.target\nBefore=network-pre.target\nWants=network-pre.target\n\n'
                '[Service]\nType=oneshot\nRemainAfterExit=yes\nTimeoutStartSec=20s\n'
                'ExecStart=/usr/bin/python3 -I ' + guard.STATE + '/boot-helper.py boot\n' + common + '\n[Install]\nWantedBy=multi-user.target\n')
        for name, content in templates.items():
            absolute = '/etc/systemd/system/' + name
            path = self.host.path(absolute)
            if path.exists() and path.read_bytes() != content.encode():
                raise guard.FirewallError('An unrecognized same-name systemd unit exists.')
            if not path.exists():
                self.core._atomic(absolute, content.encode(), {'mode': 0o644, 'uid': 0, 'gid': 0, 'xattrs': {}})
        self.runtime.atomic(guard.STATE + '/boot-helper.py', Path(guard.__file__).read_bytes())
        self.runtime.checked(['systemctl', 'daemon-reload'], timeout=8)

    def apply(self, preview):
        try:
            self._gate()
            if not isinstance(preview, dict):
                raise guard.FirewallError('A reviewed firewall plan is required.')
            supplied = dict(preview)
            fingerprint = supplied.pop('fingerprint', None)
            if fingerprint != guard.digest(guard.encoded(supplied)):
                raise guard.FirewallError('Firewall preview was modified.')
            with self.core._lock():
                self.core._check_pending()
                trusted = self._plan(preview['config'], preview['generation'])
                if trusted['fingerprint'] != fingerprint:
                    raise guard.FirewallError('Firewall or SSH state changed after preview.')
                identifier = self.core._id()
                directory = self.runtime.directory(identifier)
                self.runtime.ensure_dir(directory)
                boot_enabled = self.host.run(['systemctl', 'is-enabled', guard.BOOT_UNIT], timeout=5).returncode == 0
                before_source = self.runtime.checked(['nft', '-s', 'list', 'table', 'inet', guard.TABLE], timeout=8) if trusted['before']['exists'] else ''
                artifacts = {'candidate.nft': trusted['script'].encode(),
                             'boot.nft': self._render(trusted['config'], trusted['rescue'], False, trusted['generation']).encode(),
                             'before.nft': before_source.encode(), 'watchdog.py': Path(guard.__file__).read_bytes()}
                for name, content in artifacts.items():
                    self.runtime.atomic(directory + '/' + name, content)
                self.runtime.checked(['nft', '-c', '-f', str(self.runtime.path(directory + '/candidate.nft'))], timeout=8)
                journal = {'schema': 1, 'kind': 'firewall', 'module': 'firewall', 'transaction_id': identifier,
                           'created_ns': time.time_ns(), 'status': 'FW_PREPARED', 'config': trusted['config'],
                           'generation': trusted['generation'], 'origin': trusted['origin'], 'rescue': trusted['rescue'],
                           'before': trusted['before'], 'boot_id': self.runtime.boot_id(), 'timeout_seconds': trusted['timeout_seconds'],
                           'boot_enabled_before': boot_enabled, 'before_committed': self.runtime.read_json(guard.CONFIRMED),
                           'file_hashes': {name: guard.digest(content) for name, content in artifacts.items()}}
                self.runtime.write_journal(journal)
                self._install()
                # Boot recovery is enabled before candidate application, but it
                # can only load the previous confirmed policy, never candidate.
                self.runtime.checked(['systemctl', 'enable', guard.BOOT_UNIT], timeout=8)
                self.runtime.checked(['systemctl', 'start', self.runtime.unit('guard', identifier)], timeout=15)
                journal = self.runtime.journal(identifier)
                if journal['status'] != 'FW_ARMED' or journal.get('deadline_boottime', 0) <= guard.now() + 30:
                    raise guard.FirewallError('Independent firewall watchdog did not acknowledge readiness.')
            # No global lock is retained during kernel writes or SSH waiting.
            self.runtime.checked(['systemctl', 'start', self.runtime.unit('apply', identifier)], timeout=18)
            journal = self.runtime.journal(identifier)
            if journal['status'] != 'FW_AWAITING_NEW_SSH':
                raise guard.FirewallError('Firewall application did not reach SSH confirmation state.')
            return {'status': 'AWAITING_NEW_SSH', 'module': 'firewall', 'transaction_id': identifier,
                    'deadline_boottime': journal['deadline_boottime'],
                    'remaining_seconds': max(0, int(journal['deadline_boottime'] - guard.now())),
                    'confirmation_command': '/opt/3waServerSetting/run.sh --firewall-confirm ' + identifier + ' --confirm',
                    'message': 'Candidate active. Open a new physical SSH connection and confirm before the independent rollback deadline.'}
        except Exception as error:
            if 'identifier' in locals():
                try:
                    self.runtime.checked(['systemctl', 'start', self.runtime.unit('rollback', identifier)], timeout=18)
                except Exception:
                    pass
            raise self._error(error) from None

    def confirm(self, transaction_id):
        try:
            self._gate()
            journal = self.runtime.journal(transaction_id)
            proof = self.runtime.proof()
            if proof['socket_inode'] == journal['origin']['socket_inode'] or proof['transport_start_ticks'] <= journal.get('applied_ticks', 2 ** 63):
                raise guard.FirewallError('The applying SSH transport cannot confirm; create a new physical SSH connection.')
            self.runtime.write_json(self.runtime.directory(transaction_id) + '/confirm-request.json', proof)
            self.runtime.checked(['systemctl', 'start', self.runtime.unit('confirm', transaction_id)], timeout=18)
            if self.runtime.journal(transaction_id)['status'] != 'FW_CONFIRMED':
                raise guard.FirewallError('Firewall confirmation did not commit.')
            return {'status': 'CONFIRMED', 'module': 'firewall', 'transaction_id': transaction_id,
                    'message': 'New SSH transport verified; confirmed policy now persists at boot.'}
        except Exception as error:
            raise self._error(error) from None

    def rollback(self, transaction_id):
        try:
            self._gate()
            journal = self.runtime.journal(transaction_id)
            if journal['status'] == 'FW_CONFIRMED' and journal['before']['exists']:
                proof = self.runtime.proof()
                rescue = journal['before_committed']['rescue']
                if proof['server_port'] != rescue['port'] or not any(ipaddress.ip_address(proof['peer_ip']) in ipaddress.ip_network(source) for source in rescue['sources']):
                    raise guard.FirewallError('The current SSH source is not allowed by the previous rescue policy.')
                self.runtime.write_json(self.runtime.directory(transaction_id) + '/rollback-request.json', proof)
            self.runtime.checked(['systemctl', 'start', self.runtime.unit('rollback', transaction_id)], timeout=18)
            state = self.runtime.journal(transaction_id)['status']
            if state not in {'FW_ROLLED_BACK', 'FW_BOOT_ROLLED_BACK', 'FW_RESTORED_INITIAL', 'FW_RESTORED_PREVIOUS'}:
                raise guard.FirewallError('Firewall rollback did not complete.')
            return {'status': 'RESTORED_INITIAL' if state == 'FW_RESTORED_INITIAL' else 'RESTORED_PREVIOUS' if state == 'FW_RESTORED_PREVIOUS' else 'ROLLED_BACK',
                    'module': 'firewall', 'transaction_id': transaction_id,
                    'message': 'Previous owned firewall and boot persistence state restored; other tables were untouched.'}
        except Exception as error:
            raise self._error(error) from None

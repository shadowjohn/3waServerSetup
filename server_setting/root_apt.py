"""Explicit, reviewed APT maintenance. Package changes have no automatic rollback.

Ubuntu 26.04 command semantics:
https://manpages.ubuntu.com/manpages/resolute/man8/apt-get.8.html
https://manpages.ubuntu.com/manpages/resolute/man8/apt.8.html
"""
import hashlib
import re
import stat

from .root_actions import RootActionError, _fingerprint
from .operation_progress import phase_context

UPGRADE = ['apt-get', '--with-new-pkgs', '--no-remove',
           '-o', 'Dpkg::Options::=--force-confdef', '-o', 'Dpkg::Options::=--force-confold']
PACKAGE = r'[a-z0-9][a-z0-9+.:_-]*'
VERSION = r'[0-9A-Za-z.+:~_-]+'


def _state(host):
    digest = hashlib.sha256()
    paths = [host.path('/var/lib/dpkg/status'), host.path('/var/lib/apt/extended_states')]
    directory = host.path('/var/lib/apt/lists')
    if directory.exists():
        paths += sorted((path for path in directory.iterdir() if path.name != 'lock'), key=lambda path: path.name)
    for path in paths:
        host.path('/' + path.relative_to(host.root).as_posix())
        if not path.exists():
            digest.update((str(path.relative_to(host.root)) + ':absent\n').encode())
            continue
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise RootActionError('APT state contains an unsupported file.', 'UNSAFE_STATE')
        digest.update((path.relative_to(host.root).as_posix() + '\n').encode())
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        digest.update(b'\x00')
    return digest.hexdigest()


def _simulation(text):
    packages, kept_back = [], []
    configured = set()
    held = False
    for line in text.splitlines():
        if line.startswith('Remv '):
            raise RootActionError('APT proposed package removal; upgrade was blocked.', 'APT_UNSAFE_PLAN')
        if line.startswith('Inst '):
            match = re.match(rf'^Inst ({PACKAGE})(?: \[({VERSION})\])? \(({VERSION})(?: |\))', line)
            if match is None:
                raise RootActionError('APT simulation could not be safely summarized.', 'APT_UNSAFE_PLAN')
            packages.append(dict(zip(('name', 'installed', 'candidate'), match.groups())))
        if line.startswith('Conf '):
            match = re.match(rf'^Conf ({PACKAGE})(?: |$)', line)
            if match is None:
                raise RootActionError('APT configuration simulation is unsupported.', 'APT_UNSAFE_PLAN')
            configured.add(match.group(1))
        if line in {'The following packages have been kept back:',
                    'The following upgrades have been deferred due to phasing:'}:
            held = True
        elif held and line.startswith('  '):
            for name in line.split():
                if not re.fullmatch(PACKAGE, name):
                    raise RootActionError('APT kept-back summary is unsupported.', 'APT_UNSAFE_PLAN')
                kept_back.append(name)
        else:
            held = False
    if configured - {item['name'] for item in packages}:
        raise RootActionError('APT found pending package configuration outside this upgrade preview; inspect dpkg locally before continuing.', 'APT_UNSAFE_PLAN')
    return packages, kept_back


def plan(manager, operation):
    if operation not in {'update', 'upgrade'}:
        raise RootActionError('Only APT update or upgrade is supported.', 'INVALID_OPERATION')
    manager._gate('time')
    before = _state(manager.host)
    packages, kept_back = [], []
    command = ['apt-get', '--error-on=any', 'update']
    if operation == 'upgrade':
        simulation = manager.host.run([*UPGRADE, '--simulate', 'upgrade'], timeout=900)
        if simulation.returncode or getattr(simulation, 'output_truncated', False):
            raise RootActionError('APT upgrade simulation failed; no upgrade was started.', 'APT_SIMULATION_FAILED')
        packages, kept_back = _simulation(simulation.stdout)
        command = [*UPGRADE, '--yes', 'upgrade']
    if _state(manager.host) != before:
        raise RootActionError('APT state changed during preview; create a new preview.', 'STALE_PLAN')
    result = {'schema': 1, 'module': 'apt', 'operation': operation, 'command': command,
              'packages': packages, 'kept_back': kept_back, 'apt_state_sha256': before,
              'reboot_required': manager.host.path('/run/reboot-required').exists(),
              'notes': ['Update refreshes package indexes and uses strict error handling.',
                        'Upgrade permits new dependencies, forbids removals, and preserves modified conffiles.',
                        'Package scripts may restart services. No autoremove, distribution upgrade or reboot is run.',
                        'Package operations cannot be undone by configuration Restore last.',
                        'Package versions are a simulation; repository changes require a new preview.']}
    result['fingerprint'] = _fingerprint(result)
    return result


def run(manager, preview):
    if not isinstance(preview, dict) or preview.get('fingerprint') != _fingerprint(preview):
        raise RootActionError('APT preview was modified; create a new preview.', 'TAMPERED_PLAN')
    manager._gate('time')
    with manager._lock():
        manager._check_pending()
        trusted = plan(manager, preview.get('operation'))
        if trusted['fingerprint'] != preview['fingerprint']:
            raise RootActionError('APT state changed after preview; create a new preview.', 'STALE_PLAN')
        manager._preflight_state()
        identifier = manager._id()
        journal = {'schema': 1, 'transaction_id': identifier, 'module': 'apt', 'kind': 'apt',
                   'operation': trusted['operation'], 'status': 'APT_RUNNING',
                   'fingerprint': trusted['fingerprint'], 'packages': trusted['packages'],
                   'kept_back': trusted['kept_back']}
        manager._journal(journal)
        try:
            with phase_context('apt-' + trusted['operation']):
                completed = manager.host.run(trusted['command'], timeout=900)
            if completed.returncode:
                raise RootActionError('APT reported a nonzero exit status.', 'APT_FAILED')
        except Exception:
            journal['status'] = 'APT_FAILED'
            manager._journal(journal)
            manager._log_result({'module': 'apt', 'status': 'APT_FAILED', 'transaction_id': identifier,
                                 'message': 'APT failed; inspect the transaction and local package state.'})
            raise RootActionError('APT failed or timed out; package state may have changed. Inspect the transaction and APT locally; no automatic rollback was attempted.', 'APT_FAILED') from None
        journal['status'] = 'APT_UPDATED' if trusted['operation'] == 'update' else 'APT_UPGRADED'
        manager._journal(journal)
        return manager._log_result({'status': journal['status'], 'module': 'apt', 'operation': trusted['operation'],
                                    'transaction_id': identifier, 'packages': trusted['packages'],
                                    'kept_back': trusted['kept_back'],
                                    'reboot_required': manager.host.path('/run/reboot-required').exists(),
                                    'message': 'APT command completed. Service health is not inferred; no reboot was run.'})

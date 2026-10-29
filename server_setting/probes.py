"""Bounded local read-only probes. No SSH, package install, SQL or credential reads."""
from datetime import datetime, timezone
import os
from pathlib import Path
import platform
import re
import subprocess
from .trusted_commands import resolve, environment

MODULES = ('apache', 'php', 'vsftpd', 'samba', 'mariadb', 'ssh', 'time', 'startup', 'storage', 'firewall', 'sqlite')
SERVICES = {'apache': 'apache2.service', 'vsftpd': 'vsftpd.service',
            'samba': 'smbd.service', 'mariadb': 'mariadb.service', 'ssh': 'ssh.service'}


def parse_values(text, allowed):
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(('#', ';', '[')):
            continue
        match = re.match(r'([^=\s]+(?: [^=]+)?)\s*=\s*(.*?)\s*$', line)
        if match and match[1].strip() in allowed:
            result[match[1].strip()] = match[2].strip('"\'')
    return result


def _read(path):
    # No ~/.my.cnf, arbitrary paths, environment files or secret documents.
    try:
        file = Path(path)
        if not file.is_file() or file.stat().st_size > 1024 * 1024:
            return None
        return file.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None


def _command(argv):
    allowed = {('systemctl', 'show', service, '--no-pager', '--property=LoadState,ActiveState,UnitFileState')
               for service in SERVICES.values()}
    allowed |= {('timedatectl', 'show', '--property=Timezone', '--property=NTPSynchronized'),
                ('findmnt', '--noheadings', '--output=FSTYPE,SIZE,AVAIL', '--target', '/')}
    if tuple(argv) not in allowed:
        return None
    executable = resolve(argv[0])
    if executable is None:
        return None
    try:
        result = subprocess.run([executable, *argv[1:]], capture_output=True, text=True, timeout=4, check=False,
                                env={**environment(), 'LC_ALL': 'C', 'SYSTEMD_PAGER': 'cat'})
        return result.stdout[:65536] if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def collect(system=None):
    system = system or platform.system()
    result = {'platform': system, 'collected_at': datetime.now(timezone.utc).isoformat(),
              'root': hasattr(os, 'geteuid') and os.geteuid() == 0,
              'modules': {key: {'status': 'UNAVAILABLE', 'detail': 'Linux capability unavailable on this platform.',
                                'configured': {}, 'runtime': 'UNVERIFIED'} for key in MODULES}}
    if system != 'Linux':
        return result
    release = parse_values(_read('/etc/os-release') or '', {'ID', 'VERSION_ID'})
    result['release'] = release
    supported = release.get('ID') == 'ubuntu' and release.get('VERSION_ID') in {'22.04', '24.04', '26.04'}
    result['supported_target'] = supported  # Target only, never a claim that the host passed tests.
    for item in result['modules'].values():
        item.update(status='UNKNOWN', detail='No reliable evidence yet; no health verification performed.')
    for key, service in SERVICES.items():
        output = _command(['systemctl', 'show', service, '--no-pager', '--property=LoadState,ActiveState,UnitFileState'])
        values = parse_values(output or '', {'LoadState', 'ActiveState', 'UnitFileState'})
        if values:
            result['modules'][key].update(status='UNIT NOT FOUND' if values.get('LoadState') == 'not-found' else 'OBSERVED',
                                         detail=', '.join(f'{k}={v}' for k, v in values.items()))
    # Only known FPM ini files, with explicitly partial evidence; never PHP CLI as FPM proof.
    from .model import FIELDS
    php_candidates = sorted(Path('/etc/php').glob('*/fpm/php.ini'))
    if len(php_candidates) == 1:
        text = _read(php_candidates[0])
        if text is not None:
            result['modules']['php'].update(status='OBSERVED', configured=parse_values(text, set(FIELDS['php'])),
                detail=f'{php_candidates[0]} selected keys only; conf.d/pool overrides and effective FPM values UNVERIFIED.')
    elif php_candidates:
        result['modules']['php']['detail'] = 'Multiple FPM versions found; target selection required. Effective values UNVERIFIED.'
    for key, path in [('vsftpd', '/etc/vsftpd.conf'), ('samba', '/etc/samba/smb.conf')]:
        text = _read(path)
        if text is not None:
            result['modules'][key]['configured'] = parse_values(text, set(FIELDS[key]))
            result['modules'][key]['detail'] += f' Selected keys from {path}; include/scope/runtime not resolved.'
    time_output = _command(['timedatectl', 'show', '--property=Timezone', '--property=NTPSynchronized'])
    if time_output:
        values = parse_values(time_output, {'Timezone', 'NTPSynchronized'})
        result['modules']['time'].update(status='OBSERVED', configured=values, detail=str(values))
    swaps = _read('/proc/swaps')
    root_mount = _command(['findmnt', '--noheadings', '--output=FSTYPE,SIZE,AVAIL', '--target', '/'])
    if swaps is not None:
        # Do not expose swap path or copy raw mount tables.
        count = max(0, len(swaps.strip().splitlines()) - 1)
        result['modules']['storage'].update(status='OBSERVED', detail=f'Active swap entries: {count}; root filesystem: {(root_mount or "UNKNOWN").strip()}. LVM mapping/grow support UNVERIFIED.')
    result['modules']['startup']['detail'] = 'No startup scripts executed. Existing rc.local/systemd migration not inspected.'
    result['modules']['firewall']['detail'] = 'Offline planner only; active rules/backend/ipset/SSH rescue UNVERIFIED.'
    result['modules']['sqlite']['detail'] = 'CLI memory smoke available on explicit request; FPM and native extensions UNVERIFIED.'
    if not supported:
        result['platform_warning'] = 'Unverified/unsupported distribution; read-only observations only.'
    return result


def root_management_available():
    """Writable capabilities are enabled only for the first verified target."""
    if platform.system() != 'Linux' or not hasattr(os, 'geteuid') or os.geteuid() != 0:
        return False
    release = parse_values(_read('/etc/os-release') or '', {'ID', 'VERSION_ID'})
    return release.get('ID') == 'ubuntu' and release.get('VERSION_ID') == '26.04'

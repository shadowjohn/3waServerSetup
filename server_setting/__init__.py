"""Local version identity and shared presentation helpers; never updates a host."""
import json
from datetime import date
import os
from pathlib import Path
import platform
import re

DEVELOPMENT_VERSION = '2026.10.25.001-dev'
RELEASE_TAG = re.compile(r'v([0-9]{4})\.([0-9]{2})\.([0-9]{2})\.([0-9]{3})(?:-rc\.[1-9][0-9]*)?')
_SOURCE_ROOT = Path(__file__).resolve().parents[1]


def valid_release_tag(value):
    match = RELEASE_TAG.fullmatch(value) if isinstance(value, str) else None
    if not match or not 1 <= int(match[4]) <= 999:
        return False
    try:
        date(int(match[1]), int(match[2]), int(match[3]))
        return True
    except ValueError:
        return False


def _release_identity(root=None):
    path = (Path(root) if root is not None else _SOURCE_ROOT)/'RELEASE.json'
    if not path.exists() and not path.is_symlink():
        return None
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            raise ValueError()
        value = json.loads(path.read_text(encoding='utf-8'))
        version, commit = value['version'], value['commit']
        if (not isinstance(version, str) or not valid_release_tag('v'+version)
                or not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit)
                or type(value.get('data_schema')) is not int or value['data_schema'] != 1
                or value.get('schema', 1) != 1
                or value.get('product', '3waServerSetting') != '3waServerSetting'
                or value.get('repository', 'shadowjohn/3waServerSetup') != 'shadowjohn/3waServerSetup'
                or value.get('tag', 'v'+version) != 'v'+version
                or not isinstance(value.get('validated_platforms', []), list)
                or any(not isinstance(item, str) or not re.fullmatch(r'ubuntu(?:24|26)\.04', item)
                       for item in value.get('validated_platforms', []))):
            raise ValueError()
        return value
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        return {'invalid': True}


VERSION = (_release_identity() or {}).get('version', DEVELOPMENT_VERSION)


def revision():
    """A release uses its manifest; development Git lookup stays local only."""
    release = _release_identity()
    if release is not None:
        return 'unknown' if release.get('invalid') else release['commit'][:12]
    return _git_revision()


def _git_revision(root=None):
    import subprocess
    from .trusted_commands import resolve, environment
    binary = resolve('git')
    if not binary:
        return 'unknown'
    try:
        result = subprocess.run([binary, '-C', str(Path(root) if root is not None else _SOURCE_ROOT),
                                 'rev-parse', '--short=12', 'HEAD'], capture_output=True,
                                text=True, timeout=2, check=False, env=environment())
        value = result.stdout.strip()
        return value if result.returncode == 0 and value.isalnum() else 'unknown'
    except (OSError, subprocess.TimeoutExpired):
        return 'unknown'


def version_info(root=None):
    release = _release_identity(root)
    if release is None:
        return {'version': DEVELOPMENT_VERSION, 'commit': _git_revision(root),
                'channel': 'development', 'validated_platforms': []}
    if release.get('invalid'):
        return {'version': DEVELOPMENT_VERSION, 'commit': 'unknown',
                'channel': 'invalid-release', 'validated_platforms': []}
    return {'version': release['version'], 'commit': release['commit'], 'channel': 'release',
            'validated_platforms': release.get('validated_platforms', [])}


def update_management_available():
    """Installing program versions has a separate platform gate from services."""
    if platform.system() != 'Linux' or getattr(os, 'geteuid', lambda: -1)() != 0:
        return False
    try:
        values = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text(encoding='utf-8').splitlines()
                      if '=' in line)
        return values.get('ID', '').strip('"') == 'ubuntu' and values.get('VERSION_ID', '').strip('"') in {'24.04', '26.04'}
    except (OSError, UnicodeError):
        return False


def release_public(value):
    """Keep root filesystem staging details out of ordinary CLI/UI output."""
    fields = {'schema', 'module', 'operation', 'status', 'code', 'message', 'detail', 'supported', 'managed',
              'installed', 'previous', 'pending', 'current', 'release', 'target', 'version', 'tag', 'commit',
              'data_schema', 'validated_platforms', 'notes', 'published_at', 'archive', 'installer', 'name',
              'sha256', 'size', 'manifest_sha256', 'legacy', 'download_bytes', 'download_size', 'fingerprint', 'restart_required',
              'recovery', 'recoverable', 'signature_verified', 'verification', 'source_transaction_id', 'transaction_id'}
    if isinstance(value, dict):
        return {key: release_public(item) for key, item in value.items() if key in fields}
    if isinstance(value, (list, tuple)):
        return [release_public(item) for item in value]
    return value

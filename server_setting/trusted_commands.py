"""Shared executable trust boundary for bounded, read-only native commands."""
import os
from pathlib import Path
import platform
import re
import shutil
import stat


TRUSTED_PATH = '/usr/sbin:/usr/bin:/sbin:/bin'


def _linux_root():
    return platform.system() == 'Linux' and getattr(os, 'geteuid', lambda: -1)() == 0


def resolve(name):
    """Resolve a command name; untrusted or unavailable commands stay unknown."""
    if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.+-]*', name):
        return None
    linux = platform.system() == 'Linux'
    candidate = shutil.which(name, path=TRUSTED_PATH) if linux else shutil.which(name)
    if not candidate or not _linux_root():
        return candidate
    try:
        resolved = Path(candidate).resolve(strict=True)
        for index, path in enumerate([resolved, *resolved.parents]):
            metadata = path.stat()
            kind = stat.S_ISREG if index == 0 else stat.S_ISDIR
            if metadata.st_uid != 0 or metadata.st_mode & 0o022 or not kind(metadata.st_mode):
                return None
    except (OSError, RuntimeError):
        return None
    return candidate


def environment():
    """Root never inherits interpreter, loader, Git or shell startup controls."""
    if _linux_root():
        return {'PATH': TRUSTED_PATH, 'LC_ALL': 'C', 'LANG': 'C', 'HOME': '/root',
                'SYSTEMD_PAGER': 'cat', 'GIT_CONFIG_NOSYSTEM': '1',
                'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_OPTIONAL_LOCKS': '0'}
    env = dict(os.environ)
    if platform.system() == 'Linux':
        env['PATH'] = TRUSTED_PATH
    return env

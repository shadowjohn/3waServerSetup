"""Explicit local memory-only PHP smoke; never automatically loads native modules."""
from pathlib import Path
import re
import stat
from .trusted_commands import resolve, environment, _linux_root
import subprocess


def sqlite_smoke():
    binary = resolve('php')
    if not binary:
        return 'UNAVAILABLE: PHP CLI is not installed; no installation attempted.'
    path = Path(__file__).resolve().parents[1] / 'tests/sqlite-smoke.php'
    try:
        result = subprocess.run([binary, '-d', 'auto_prepend_file=', '-d', 'auto_append_file=', str(path)], capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=20, check=False, env=environment())
    except (OSError, subprocess.TimeoutExpired) as error:
        return f'UNVERIFIED: local CLI smoke did not complete ({type(error).__name__}).'
    return f'CLI ONLY / exit={result.returncode}; FPM UNVERIFIED\n' + (result.stdout + result.stderr)[:16000]


def _root_spatialite_path(path):
    """追蹤官方 .so 與自有 alias 的完整鏈，不能以 realpath 跳過中間來源。"""
    def require_controlled(metadata, kind):
        if metadata.st_uid != 0:
            raise ValueError('Root diagnostics require a root-owned native module, every symlink and parent directory.')
        if metadata.st_mode & 0o022 or not kind(metadata.st_mode):
            raise ValueError('Root diagnostics refuse group/world-writable native modules or parent directories.')

    current = Path(path.anchor)
    pending = list(path.parts[1:])
    links = 0
    require_controlled(current.lstat(), stat.S_ISDIR)
    while pending:
        part = pending.pop(0)
        if part == '..':
            current = current.parent
            require_controlled(current.lstat(), stat.S_ISDIR)
            continue
        if part == '.':
            continue
        candidate = current / part
        metadata = candidate.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            if metadata.st_uid != 0:
                raise ValueError('Root diagnostics refuse a non-root-owned intermediate module symlink.')
            links += 1
            if links > 40:
                raise ValueError('The native module symlink chain exceeds its inspection limit.')
            target = candidate.readlink()
            if target.is_absolute():
                current = Path(target.anchor)
                require_controlled(current.lstat(), stat.S_ISDIR)
                pending = list(target.parts[1:]) + pending
            else:
                pending = list(target.parts) + pending
            continue
        require_controlled(metadata, stat.S_ISDIR if pending else stat.S_ISREG)
        current = candidate
    require_controlled(current.lstat(), stat.S_ISREG)
    return current


def _trusted_spatialite_module(module):
    if (not isinstance(module, str) or not module or len(module) > 4096
            or any(char in module for char in '\0\r\n') or module.startswith(('//', '\\\\'))):
        raise ValueError('Supply an absolute path to a trusted local native SpatiaLite module.')
    path = Path(module)
    if not path.is_absolute() or not re.search(r'\.(?:so(?:\.[0-9]+)*|dll|dylib)$', path.name, re.I):
        raise ValueError('Module must be an absolute local native-library path, such as /var/www/sqlite3_ext/mod_spatialite.so.')
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise ValueError('The supplied native module is not a regular local file.')
        if _linux_root():
            if _root_spatialite_path(path) != resolved:
                raise ValueError('Native module resolution changed during inspection; retry with its canonical path.')
    except (OSError, RuntimeError):
        raise ValueError('Native module is missing or its local path cannot be inspected.') from None
    return str(resolved)


def sqlite_spatial_smoke(module, acceptance=False):
    """明確選擇可信模組後，執行既有 CLI SQL／RTree 或雙驅動驗收。"""
    if module is None or module == '':
        return 'UNVERIFIED: no module selected; supply a trusted local native SpatiaLite library. No native module was loaded.'
    try:
        if type(acceptance) is not bool:
            raise ValueError('Select quick spatial queries or the full acceptance workload.')
        checked = _trusted_spatialite_module(module)
    except ValueError as error:
        return 'FAIL: ' + str(error)
    binary = resolve('php')
    if not binary:
        return 'UNAVAILABLE: PHP CLI is not installed; no installation attempted.'
    path = Path(__file__).resolve().parents[1] / 'tests/sqlite-smoke.php'
    arguments = ['--acceptance', '--module=' + checked] if acceptance else [
        '--module=' + checked, '--loader=legacy-sql']
    try:
        result = subprocess.run([binary, '-d', 'auto_prepend_file=', '-d', 'auto_append_file=',
                                 str(path), *arguments], capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=300 if acceptance else 30,
                                check=False, env=environment())
    except (OSError, subprocess.TimeoutExpired) as error:
        return f'UNVERIFIED: local CLI spatial test did not complete ({type(error).__name__}); FPM UNVERIFIED.'
    return f'CLI ONLY / exit={result.returncode}; FPM UNVERIFIED\n' + (result.stdout + result.stderr)[:32768]

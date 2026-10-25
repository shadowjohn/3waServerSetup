"""Explicit local memory-only PHP smoke; never automatically loads native modules."""
from pathlib import Path
from .trusted_commands import resolve, environment
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

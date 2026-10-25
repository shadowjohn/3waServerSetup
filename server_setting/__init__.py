"""3waServerSetting local read-only TUI; no system mutation API."""
VERSION = '0.1.0-dev'


def revision():
    """Best-effort local Git identity only; never fetches or changes the tree."""
    from pathlib import Path
    import subprocess
    from .trusted_commands import resolve, environment
    binary = resolve('git')
    if not binary:
        return 'unknown'
    try:
        result = subprocess.run([binary, '-C', str(Path(__file__).resolve().parents[1]),
                                 'rev-parse', '--short=12', 'HEAD'], capture_output=True,
                                text=True, timeout=2, check=False, env=environment())
        value = result.stdout.strip()
        return value if result.returncode == 0 and value.isalnum() else 'unknown'
    except (OSError, subprocess.TimeoutExpired):
        return 'unknown'

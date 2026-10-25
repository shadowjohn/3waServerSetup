#!/bin/sh
# Explicit USER-directory acceptance only. No sudo, SSH, apt or system apply.
set -eu
umask 077
if [ "$(id -u)" -eq 0 ]; then
    echo "STOP: run as an ordinary test user, not root." >&2
    exit 1
fi
test_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd -- "$test_dir"
python3 -c 'import platform,sys; r=platform.freedesktop_os_release(); print("Host:",platform.system(),r.get("ID"),r.get("VERSION_ID")); sys.exit(0 if r.get("ID")=="ubuntu" and r.get("VERSION_ID")=="26.04" else "STOP: this acceptance target is Ubuntu 26.04")'
if [ ! -x .venv/bin/python ]; then
    if ! python3 -m venv .venv; then
        echo "STOP: project venv creation failed. Ask the user; do not install system packages." >&2
        exit 1
    fi
fi
.venv/bin/python -c 'import sys; assert sys.prefix != sys.base_prefix, "STOP: not using a venv"'
.venv/bin/python -m pip --isolated install --require-hashes --no-deps --only-binary=:all: --index-url https://pypi.org/simple --disable-pip-version-check -r requirements-tui.lock
.venv/bin/python -m pip check
mkdir -p validation-results
.venv/bin/python -m server_setting --status-json > validation-results/status.json
.venv/bin/python -m unittest discover -s tests > validation-results/tests.txt 2>&1
.venv/bin/python -m server_setting --smoke > validation-results/ui-smoke.txt 2>&1
.venv/bin/python -m compileall -q server_setting
echo "Acceptance commands completed. Review status.json: OBSERVED is not a health PASS."
echo "Reports: validation-results/ (local only). Next: .venv/bin/python -m server_setting"

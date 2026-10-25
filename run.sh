#!/bin/sh
# Local console launcher. Root changes require an explicit UI/CLI action.
set -eu
# The root console must not resolve tools from a caller's writable PATH.
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
tool_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd -- "$tool_dir"
if [ -x "$tool_dir/.venv/bin/python" ]; then
    exec "$tool_dir/.venv/bin/python" -E -s -m server_setting "$@"
fi
exec python3 -E -s -m server_setting "$@"

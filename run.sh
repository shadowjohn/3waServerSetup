#!/bin/sh
# Local console launcher. Root changes require an explicit UI/CLI action.
set -eu
# The root console must not resolve tools from a caller's writable PATH.
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
tool_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# Freeze the physical version once; venv and imports must come from that same
# directory even if another process changes current before Python starts.
if [ -L "$tool_dir/current" ]; then
    release_dir=$(CDPATH= cd -- "$tool_dir/current" && pwd -P)
    case "$release_dir" in
        "$tool_dir"/releases/*) exec "$release_dir/run.sh" "$@" ;;
        *) echo 'UNSAFE: current is outside the managed releases directory.' >&2; exit 2 ;;
    esac
fi
cd -- "$tool_dir"
if [ -x "$tool_dir/.venv/bin/python" ]; then
    exec "$tool_dir/.venv/bin/python" -E -s -m server_setting "$@"
fi
exec python3 -E -s -m server_setting "$@"

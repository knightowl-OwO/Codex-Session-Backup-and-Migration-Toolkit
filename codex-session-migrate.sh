#!/usr/bin/env bash
set -eu

script_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
for python in python3 python3.14 python3.13 python3.12 python3.11; do
    if command -v "$python" >/dev/null 2>&1 &&
        "$python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        exec "$python" "$script_dir/codex_session_migrate.py" "$@"
    fi
done

printf '%s\n' 'Codex session migration requires Python 3.11 or newer.' >&2
exit 1

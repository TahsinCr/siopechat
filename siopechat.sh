#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -x "$ROOT/python/bin/python" ]; then
    exec "$ROOT/python/bin/python" "$ROOT/siopechat.py" "$@"
fi
if command -v python3 >/dev/null 2>&1; then
    exec python3 "$ROOT/siopechat.py" "$@"
fi
echo "SiopeChat requires Python 3.10 or newer." >&2
exit 1

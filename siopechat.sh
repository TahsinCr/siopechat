#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

resolve_venv() {
    if [ "${SIOPECHAT_VENV+x}" = x ]; then
        if [ -z "$SIOPECHAT_VENV" ]; then
            echo "SIOPECHAT_VENV is set but empty. Set it to a virtual-environment directory." >&2
            exit 1
        fi
        case "$SIOPECHAT_VENV" in
            /*) VENV_DIR=$SIOPECHAT_VENV ;;
            *) VENV_DIR="$ROOT/$SIOPECHAT_VENV" ;;
        esac
        VENV_PYTHON="$VENV_DIR/bin/python"
        if [ ! -x "$VENV_PYTHON" ]; then
            echo "The SIOPECHAT_VENV environment has no executable at $VENV_PYTHON." >&2
            exit 1
        fi
        return
    fi

    for VENV_NAME in python .venv venv env; do
        VENV_PYTHON="$ROOT/$VENV_NAME/bin/python"
        if [ -x "$VENV_PYTHON" ]; then
            return
        fi
    done

    echo "No Python environment found. Run '$ROOT/install.sh' first." >&2
    exit 1
}

resolve_venv
if ! "$VENV_PYTHON" -c 'import sys; raise SystemExit(0 if sys.prefix != sys.base_prefix and sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    PYTHON_VERSION=$($VENV_PYTHON --version 2>&1 || true)
    echo "Expected a Python 3.11+ virtual environment; found ${PYTHON_VERSION:-an unusable interpreter}." >&2
    echo "Run the installer or set SIOPECHAT_VENV to a virtual-environment directory." >&2
    exit 1
fi

exec "$VENV_PYTHON" "$ROOT/siopechat.py" "$@"

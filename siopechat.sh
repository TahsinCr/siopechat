#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

check_node() {
    if ! command -v node >/dev/null 2>&1; then
        echo "SiopeChat requires Node.js 22 or newer. Install Node.js and try again." >&2
        exit 1
    fi

    NODE_VERSION=$(node --version 2>/dev/null || true)
    NODE_MAJOR=${NODE_VERSION#v}
    NODE_MAJOR=${NODE_MAJOR%%.*}
    case "$NODE_MAJOR" in
        ''|*[!0-9]*)
            echo "Could not read the Node.js version (reported: ${NODE_VERSION:-unknown})." >&2
            exit 1
            ;;
    esac
    if [ "$NODE_MAJOR" -lt 22 ]; then
        echo "SiopeChat requires Node.js 22 or newer; found $NODE_VERSION." >&2
        exit 1
    fi
}

check_node

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
if ! "$VENV_PYTHON" -c 'import sys; raise SystemExit(0 if sys.prefix != sys.base_prefix and sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    PYTHON_VERSION=$($VENV_PYTHON --version 2>&1 || true)
    echo "Expected a Python 3.10+ virtual environment; found ${PYTHON_VERSION:-an unusable interpreter}." >&2
    echo "Run the installer or set SIOPECHAT_VENV to a virtual-environment directory." >&2
    exit 1
fi

exec "$VENV_PYTHON" "$ROOT/siopechat.py" "$@"

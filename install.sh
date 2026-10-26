#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

fail() {
    echo "SiopeChat installer: $*" >&2
    exit 1
}

check_node() {
    command -v node >/dev/null 2>&1 || fail "Node.js 22 or newer is required. Install Node.js and try again."
    NODE_VERSION=$(node --version 2>/dev/null) || fail "Could not run node --version."
    NODE_MAJOR=${NODE_VERSION#v}
    NODE_MAJOR=${NODE_MAJOR%%.*}
    case "$NODE_MAJOR" in
        ''|*[!0-9]*) fail "Could not read the Node.js version (reported: $NODE_VERSION)." ;;
    esac
    [ "$NODE_MAJOR" -ge 22 ] || fail "Node.js 22 or newer is required; found $NODE_VERSION."
}

python_supported() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

venv_supported() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.prefix != sys.base_prefix and sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

check_node

SYSTEM_PYTHON=
for PYTHON_COMMAND in python3 python; do
    if command -v "$PYTHON_COMMAND" >/dev/null 2>&1 && python_supported "$PYTHON_COMMAND"; then
        SYSTEM_PYTHON=$(command -v "$PYTHON_COMMAND")
        break
    fi
done
[ -n "$SYSTEM_PYTHON" ] || fail "Python 3.11 or newer is required to create the environment."

CREATE_ENV=0
if [ "${SIOPECHAT_VENV+x}" = x ]; then
    [ -n "$SIOPECHAT_VENV" ] || fail "SIOPECHAT_VENV is set but empty; set it to a virtual-environment directory."
    case "$SIOPECHAT_VENV" in
        /*) VENV_DIR=$SIOPECHAT_VENV ;;
        *) VENV_DIR="$ROOT/$SIOPECHAT_VENV" ;;
    esac
    VENV_PYTHON="$VENV_DIR/bin/python"
    if [ -x "$VENV_PYTHON" ]; then
        venv_supported "$VENV_PYTHON" || fail "The requested environment must be a Python 3.11+ virtual environment; it was left untouched."
    elif [ -e "$VENV_DIR" ]; then
        fail "The requested environment exists but has no executable at $VENV_PYTHON; it was left untouched."
    else
        CREATE_ENV=1
    fi
else
    for VENV_NAME in python .venv venv env; do
        CANDIDATE="$ROOT/$VENV_NAME/bin/python"
        if [ -x "$CANDIDATE" ] && venv_supported "$CANDIDATE"; then
            VENV_DIR="$ROOT/$VENV_NAME"
            VENV_PYTHON=$CANDIDATE
            break
        fi
    done

    if [ -z "${VENV_DIR:-}" ]; then
        VENV_DIR="$ROOT/python"
        VENV_PYTHON="$VENV_DIR/bin/python"
        if [ -e "$VENV_DIR" ]; then
            fail "'$VENV_DIR' exists but is not a usable virtual environment; it was left untouched. Set SIOPECHAT_VENV to another path."
        fi
        CREATE_ENV=1
    fi
fi

if [ "$CREATE_ENV" -eq 1 ]; then
    echo "Creating Python environment at $VENV_DIR"
    "$SYSTEM_PYTHON" -m venv "$VENV_DIR" || fail "Could not create the virtual environment."
fi

[ -x "$VENV_PYTHON" ] || fail "The environment does not contain an executable at $VENV_PYTHON."
if ! venv_supported "$VENV_PYTHON"; then
    PYTHON_VERSION=$("$VENV_PYTHON" --version 2>&1 || true)
    fail "The selected environment must use Python 3.11 or newer (found ${PYTHON_VERSION:-an unusable interpreter})."
fi

echo "Installing SiopeChat and its Python dependencies in $VENV_DIR"
"$VENV_PYTHON" -m pip install --disable-pip-version-check --editable "$ROOT" || fail "Dependency installation failed. The environment was kept so you can retry after fixing the cause."
echo "Installation complete. Start SiopeChat with: $ROOT/siopechat.sh"

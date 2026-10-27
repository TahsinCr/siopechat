"""Resolve checkout and per-user application storage paths."""

import getpass
import os
import sys
import tempfile
from pathlib import Path

APP_DIR = "SiopeChat"
# This is a persisted XDG namespace, not a source-checkout directory.
XDG_APP_DATA_DIR = "chatgpt-web"


def _xdg_path(variable: str, fallback: Path, app_name: str) -> Path:
    configured = os.environ.get(variable)
    return (Path(configured) if configured else fallback) / app_name


def _local_app_data() -> Path:
    configured = os.environ.get("LOCALAPPDATA")
    root = Path(configured) if configured else Path.home() / "AppData" / "Local"
    return root / APP_DIR


def user_data_dir() -> Path:
    """Return the per-user data location, independent of the launch directory."""
    if os.name == "nt":
        return _local_app_data()
    if sys.platform == "darwin" and not os.environ.get("XDG_DATA_HOME"):
        return Path.home() / "Library" / "Application Support" / APP_DIR
    return _xdg_path("XDG_DATA_HOME", Path.home() / ".local" / "share", XDG_APP_DATA_DIR)


def state_dir() -> Path:
    if os.name == "nt":
        return _local_app_data()
    if sys.platform == "darwin" and not os.environ.get("XDG_STATE_HOME"):
        return Path.home() / "Library" / "Application Support" / APP_DIR
    return _xdg_path("XDG_STATE_HOME", Path.home() / ".local" / "state", XDG_APP_DATA_DIR)


def cache_dir() -> Path:
    if os.name == "nt":
        return _local_app_data() / "Cache"
    if sys.platform == "darwin" and not os.environ.get("XDG_CACHE_HOME"):
        return Path.home() / "Library" / "Caches" / APP_DIR
    return _xdg_path("XDG_CACHE_HOME", Path.home() / ".cache", XDG_APP_DATA_DIR)


def runtime_dir() -> Path:
    if os.name == "nt":
        # AF_UNIX paths on Windows are short; TEMP already has per-user ACLs.
        return Path(tempfile.gettempdir()) / f"sc-{getpass.getuser()[:16]}"
    configured = os.environ.get("XDG_RUNTIME_DIR")
    if configured:
        return Path(configured) / XDG_APP_DATA_DIR
    return Path(tempfile.gettempdir()) / f"{XDG_APP_DATA_DIR}-{os.getuid()}"


def application_root() -> Path:
    """Use the source checkout in development and user data when installed.

    The checkout is discovered from this module's location, never from the
    process working directory. Installed applications therefore keep their
    configuration and databases in a stable per-user location.
    """
    configured = os.environ.get("SIOPECHAT_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    checkout = Path(__file__).resolve().parents[2]
    if (checkout / "pyproject.toml").is_file() and (checkout / "src" / "bootstrap.py").is_file():
        return checkout
    return user_data_dir()

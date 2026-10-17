"""Application paths, including project-local data and legacy session locations."""

import getpass
import os
import sys
import tempfile
from pathlib import Path

LEGACY_APP_DIR = "chatgpt-web"
APP_DIR = "SiopeChat"


def _xdg_path(variable: str, fallback: Path, app_name: str) -> Path:
    configured = os.environ.get(variable)
    return (Path(configured) if configured else fallback) / app_name


def _local_app_data() -> Path:
    configured = os.environ.get("LOCALAPPDATA")
    root = Path(configured) if configured else Path.home() / "AppData" / "Local"
    return root / APP_DIR


def legacy_data_dir() -> Path:
    if os.name == "nt":
        return _local_app_data()
    if sys.platform == "darwin" and not os.environ.get("XDG_DATA_HOME"):
        return Path.home() / "Library" / "Application Support" / APP_DIR
    return _xdg_path("XDG_DATA_HOME", Path.home() / ".local" / "share", LEGACY_APP_DIR)


def state_dir() -> Path:
    if os.name == "nt":
        return _local_app_data()
    if sys.platform == "darwin" and not os.environ.get("XDG_STATE_HOME"):
        return Path.home() / "Library" / "Application Support" / APP_DIR
    return _xdg_path("XDG_STATE_HOME", Path.home() / ".local" / "state", LEGACY_APP_DIR)


def cache_dir() -> Path:
    if os.name == "nt":
        return _local_app_data() / "Cache"
    if sys.platform == "darwin" and not os.environ.get("XDG_CACHE_HOME"):
        return Path.home() / "Library" / "Caches" / APP_DIR
    return _xdg_path("XDG_CACHE_HOME", Path.home() / ".cache", LEGACY_APP_DIR)


def runtime_dir() -> Path:
    if os.name == "nt":
        # AF_UNIX paths on Windows are short; TEMP already has per-user ACLs.
        return Path(tempfile.gettempdir()) / f"sc-{getpass.getuser()[:16]}"
    configured = os.environ.get("XDG_RUNTIME_DIR")
    if configured:
        return Path(configured) / LEGACY_APP_DIR
    return Path(tempfile.gettempdir()) / f"chatgpt-web-{os.getuid()}"


def project_root() -> Path:
    configured = os.environ.get("SIOPECHAT_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    checkout = Path(__file__).resolve().parents[2]
    return checkout if (checkout / "pyproject.toml").is_file() else Path.cwd()


def data_dir() -> Path:
    return project_root() / "data"

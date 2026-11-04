"""Resolve per-user application storage and platform runtime paths."""

import getpass
import os
import shutil
import sys
import tempfile
from pathlib import Path

_APPLICATION_NAME = "siopechat"


def _xdg_path(variable: str, fallback: Path) -> Path:
    configured = os.environ.get(variable)
    return (Path(configured) if configured else fallback) / _APPLICATION_NAME


def _local_app_data() -> Path:
    configured = os.environ.get("LOCALAPPDATA")
    root = Path(configured) if configured else Path.home() / "AppData" / "Local"
    return root / _APPLICATION_NAME


def user_data_dir() -> Path:
    """Return the per-user data location, independent of the launch directory."""
    if os.name == "nt":
        return _local_app_data()
    if sys.platform == "darwin" and not os.environ.get("XDG_DATA_HOME"):
        return Path.home() / "Library" / "Application Support" / _APPLICATION_NAME
    return _xdg_path("XDG_DATA_HOME", Path.home() / ".local" / "share")


def state_dir() -> Path:
    if os.name == "nt":
        return _local_app_data()
    if sys.platform == "darwin" and not os.environ.get("XDG_STATE_HOME"):
        return Path.home() / "Library" / "Application Support" / _APPLICATION_NAME
    return _xdg_path("XDG_STATE_HOME", Path.home() / ".local" / "state")


def cache_dir() -> Path:
    if os.name == "nt":
        return _local_app_data() / "Cache"
    if sys.platform == "darwin" and not os.environ.get("XDG_CACHE_HOME"):
        return Path.home() / "Library" / "Caches" / _APPLICATION_NAME
    return _xdg_path("XDG_CACHE_HOME", Path.home() / ".cache")


def runtime_dir() -> Path:
    if os.name == "nt":
        # AF_UNIX paths on Windows are short; TEMP already has per-user ACLs.
        return Path(tempfile.gettempdir()) / f"sc-{getpass.getuser()[:16]}"
    configured = os.environ.get("XDG_RUNTIME_DIR")
    if configured:
        return Path(configured) / _APPLICATION_NAME
    return Path(tempfile.gettempdir()) / f"{_APPLICATION_NAME}-{os.getuid()}"


def application_root() -> Path:
    """Return the stable per-user storage root, unless explicitly overridden."""
    configured = os.environ.get("SIOPECHAT_HOME")
    return Path(configured).expanduser().resolve() if configured else user_data_dir()


def migrate_checkout_data(checkout: Path, root: Path) -> None:
    """Copy legacy source-checkout data into per-user storage without deleting it."""
    if os.environ.get("SIOPECHAT_HOME"):
        return

    checkout = Path(checkout).resolve()
    root = Path(root).resolve()
    if (
        checkout == root
        or not (checkout / "pyproject.toml").is_file()
        or not (checkout / "src" / "bootstrap.py").is_file()
    ):
        return

    sources = ("config.json", "data", "downloads")
    if not any((checkout / name).exists() for name in sources):
        return

    root.mkdir(parents=True, exist_ok=True)
    for name in sources:
        source = checkout / name
        destination = root / name
        if not source.exists() or source.is_symlink() or destination.exists():
            continue

        staging = Path(tempfile.mkdtemp(prefix=f".{name}-migration-", dir=root))
        try:
            if source.is_dir():
                shutil.copytree(source, staging, dirs_exist_ok=True, symlinks=True)
                try:
                    os.rename(staging, destination)
                except OSError:
                    if not destination.exists():
                        raise
            else:
                staged_file = staging / name
                shutil.copy2(source, staged_file)
                try:
                    os.link(staged_file, destination)
                except FileExistsError:
                    pass
        finally:
            shutil.rmtree(staging, ignore_errors=True)

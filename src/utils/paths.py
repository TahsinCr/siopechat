"""Resolve standard user storage paths with independent portable overrides."""

import os
from pathlib import Path

from platformdirs import PlatformDirs

_APP_DIRS = PlatformDirs("siopechat", appauthor=False)
_PORTABLE_VALUES = {"1", "true", "yes", "on"}


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _root(root: Path | None) -> Path:
    return Path(root).expanduser().resolve() if root is not None else project_root()


def portable_mode() -> bool:
    return os.environ.get("SIOPECHAT_PORTABLE", "").strip().casefold() in _PORTABLE_VALUES


def _default(root: Path | None, portable_name: str, standard: Path) -> Path:
    return _root(root) / portable_name if root is not None or portable_mode() else standard


def _path(variable: str, default: Path, root: Path | None) -> Path:
    value = os.environ.get(variable)
    if not value:
        return default
    path = Path(value).expanduser()
    return (path if path.is_absolute() else _root(root) / path).resolve()


def config_path(root: Path | None = None) -> Path:
    default = _default(root, "config.json", _APP_DIRS.user_config_path / "config.json")
    return _path("SIOPECHAT_CONFIG_PATH", default, root)


def data_dir(root: Path | None = None) -> Path:
    default = _default(root, "data", _APP_DIRS.user_data_path)
    return _path("SIOPECHAT_DATA_DIR", default, root)


def state_dir(root: Path | None = None) -> Path:
    default = _default(root, "data", _APP_DIRS.user_state_path)
    return _path("SIOPECHAT_STATE_DIR", default, root)


def downloads_dir(root: Path | None = None) -> Path:
    default = _default(root, "downloads", _APP_DIRS.user_downloads_path)
    return _path("SIOPECHAT_DOWNLOADS_DIR", default, root)


def cache_dir(root: Path | None = None) -> Path:
    default = _default(root, ".cache", _APP_DIRS.user_cache_path)
    return _path("SIOPECHAT_CACHE_DIR", default, root)


def runtime_dir(root: Path | None = None) -> Path:
    default = _default(root, ".runtime", _APP_DIRS.user_runtime_path)
    return _path("SIOPECHAT_RUNTIME_DIR", default, root)


def ensure_private_dir(path: Path) -> Path:
    path = Path(path)
    created = not path.exists()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if created and os.name != "nt":
        path.chmod(0o700)
    return path


def ensure_runtime_dir(path: Path | None = None) -> Path:
    path = Path(path) if path is not None else runtime_dir()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)
    return path

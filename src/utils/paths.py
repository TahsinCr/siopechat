"""Resolve checkout and per-user application storage paths."""

import getpass
import importlib.metadata
import os
import re
import sys
import tempfile
import tomllib
from functools import lru_cache
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve()


@lru_cache(maxsize=1)
def _application_data_name() -> str:
    """Return the filesystem name declared by this application's package."""
    checkout = _MODULE_PATH.parents[2]
    project_file = checkout / "pyproject.toml"
    if project_file.is_file() and (checkout / "src" / "bootstrap.py").is_file():
        project = tomllib.loads(project_file.read_text(encoding="utf-8"))["project"]
        project_name = project["name"]
    else:
        project_name = None
        module_path = os.path.normcase(os.path.realpath(_MODULE_PATH))
        for distribution in importlib.metadata.distributions():
            for entry in distribution.files or ():
                entry_parts = tuple(str(entry).replace("\\", "/").split("/"))
                installed_path = os.path.normcase(os.path.realpath(distribution.locate_file(entry)))
                if entry_parts[-2:] == ("utils", "paths.py") and installed_path == module_path:
                    project_name = distribution.metadata["Name"]
                    break
            if project_name:
                break
    if not project_name:
        raise RuntimeError("Cannot resolve the installed application package name")
    return re.sub(r"[-_.]+", "-", project_name.strip()).lower()


def _xdg_path(variable: str, fallback: Path, app_name: str) -> Path:
    configured = os.environ.get(variable)
    return (Path(configured) if configured else fallback) / app_name


def _local_app_data() -> Path:
    configured = os.environ.get("LOCALAPPDATA")
    root = Path(configured) if configured else Path.home() / "AppData" / "Local"
    return root / _application_data_name()


def user_data_dir() -> Path:
    """Return the per-user data location, independent of the launch directory."""
    if os.name == "nt":
        return _local_app_data()
    if sys.platform == "darwin" and not os.environ.get("XDG_DATA_HOME"):
        return Path.home() / "Library" / "Application Support" / _application_data_name()
    return _xdg_path("XDG_DATA_HOME", Path.home() / ".local" / "share", _application_data_name())


def state_dir() -> Path:
    if os.name == "nt":
        return _local_app_data()
    if sys.platform == "darwin" and not os.environ.get("XDG_STATE_HOME"):
        return Path.home() / "Library" / "Application Support" / _application_data_name()
    return _xdg_path("XDG_STATE_HOME", Path.home() / ".local" / "state", _application_data_name())


def cache_dir() -> Path:
    if os.name == "nt":
        return _local_app_data() / "Cache"
    if sys.platform == "darwin" and not os.environ.get("XDG_CACHE_HOME"):
        return Path.home() / "Library" / "Caches" / _application_data_name()
    return _xdg_path("XDG_CACHE_HOME", Path.home() / ".cache", _application_data_name())


def runtime_dir() -> Path:
    if os.name == "nt":
        # AF_UNIX paths on Windows are short; TEMP already has per-user ACLs.
        return Path(tempfile.gettempdir()) / f"sc-{getpass.getuser()[:16]}"
    configured = os.environ.get("XDG_RUNTIME_DIR")
    if configured:
        return Path(configured) / _application_data_name()
    return Path(tempfile.gettempdir()) / f"{_application_data_name()}-{os.getuid()}"


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

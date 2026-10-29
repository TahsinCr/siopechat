"""Typed, package-local script paths shared by the Python backend."""

from enum import Enum
from pathlib import Path


class Script(str, Enum):
    SIGN = "sentinel/sign.cjs"


def script_path(script: Script) -> Path:
    return Path(__file__).resolve().parent / "scripts" / script.value

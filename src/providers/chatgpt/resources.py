"""Typed, package-local script paths shared by the Python backend."""

from enum import Enum
from pathlib import Path


class Script(str, Enum):
    AUTH = "browser/auth.cjs"
    SEND = "browser/send.cjs"
    CAPTURE = "browser/capture.cjs"
    SIGN = "sentinel/sign.cjs"


def script_path(script: Script) -> Path:
    return Path(__file__).resolve().parent / "scripts" / script.value

"""Expose supported standard-library features across Python versions."""

from __future__ import annotations

import sys


if sys.version_info >= (3, 11):
    from builtins import ExceptionGroup
    from enum import StrEnum
else:  # pragma: no cover - exercised by the Python 3.10 CI job
    from backports.strenum import StrEnum
    from exceptiongroup import ExceptionGroup


__all__ = ["ExceptionGroup", "StrEnum"]

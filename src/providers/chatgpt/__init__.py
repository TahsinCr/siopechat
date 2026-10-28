"""Standalone ChatGPT backend; all session paths are supplied by callers."""

from .client import ChatGPTClient
from .errors import ChatGPTWebError, SentinelCompatibilityError
from .session import BrowserSession, SessionPaths

__all__ = [
    "BrowserSession",
    "ChatGPTClient",
    "ChatGPTWebError",
    "SentinelCompatibilityError",
    "SessionPaths",
    "ChatGPTProvider",
]

from .adapter import ChatGPTProvider

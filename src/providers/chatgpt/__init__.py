"""Standalone ChatGPT backend; all session paths are supplied by callers."""

from .client import ChatGPTClient
from .session import BrowserSession, SessionPaths

__all__ = ["BrowserSession", "ChatGPTClient", "SessionPaths", "ChatGPTProvider"]

from .adapter import ChatGPTProvider

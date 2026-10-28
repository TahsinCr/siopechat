"""Independent Gemini web-session adapter (no paid model API)."""

from .adapter import GeminiProvider
from .errors import GeminiCompatibilityError, GeminiError, GeminiStreamError

__all__ = [
    "GeminiCompatibilityError",
    "GeminiError",
    "GeminiProvider",
    "GeminiStreamError",
    "Notebooks",
]

from .notebooks import Notebooks

"""Independent Gemini web-session adapter (no paid model API)."""

from .errors import GeminiCompatibilityError, GeminiError, GeminiStreamError
from .adapter import GeminiProvider

__all__ = [
    "GeminiCompatibilityError",
    "GeminiError",
    "GeminiProvider",
    "GeminiStreamError",
    "Notebooks",
]

from .notebooks import Notebooks

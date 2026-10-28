"""Gemini web-session failures exposed through the provider contract."""

from ..errors import ProviderError


class GeminiError(ProviderError):
    code = "gemini_error"


class GeminiCompatibilityError(GeminiError):
    code = "gemini_compatibility"


class GeminiStreamError(GeminiError):
    code = "gemini_stream_error"

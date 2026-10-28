"""Claude web-session failures exposed through the provider contract."""

from ..errors import ProviderError


class ClaudeError(ProviderError):
    code = "claude_error"


class ClaudeCompatibilityError(ClaudeError):
    code = "claude_compatibility"


class ClaudeRequestNotSent(ClaudeError):
    code = "claude_request_not_sent"
    message_submitted = False


class ClaudeStreamError(ClaudeError):
    code = "claude_stream_error"

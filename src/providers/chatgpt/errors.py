from ..errors import ProviderError


class ChatGPTWebError(ProviderError):
    code = "chatgpt_error"


class SentinelCompatibilityError(ChatGPTWebError):
    """The local proof helper cannot satisfy the current web verification flow."""

    code = "chatgpt_sentinel_incompatible"
    message_submitted = False

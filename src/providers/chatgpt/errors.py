from ..errors import ProviderError


class ChatGPTWebError(ProviderError):
    pass


class SentinelCompatibilityError(ChatGPTWebError):
    pass

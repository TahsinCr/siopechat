"""Provider-independent failures without credentials or raw transport payloads."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .api import Capability


class ProviderError(RuntimeError):
    pass


class FeatureUnavailable(ProviderError):
    def __init__(self, capability, reason: str = "unsupported"):
        super().__init__(f"{capability.value}: {reason}")
        self.capability = capability
        self.reason = reason


class RateLimited(ProviderError):
    def __init__(self, retry_after: float | None = None):
        suffix = (
            f"; retry after {retry_after:g} seconds"
            if isinstance(retry_after, (int, float))
            else ""
        )
        super().__init__(f"provider rate limit{suffix}")
        self.retry_after = retry_after


class AccountPaused(ProviderError):
    """Login or user verification is needed before new sends are allowed."""


class CompatibilityError(ProviderError):
    def __init__(self, capability: Capability):
        super().__init__("provider capability changed")
        self.capability = capability

"""Provider-independent failures without credentials or raw transport payloads."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .api import Capability


class ProviderError(RuntimeError):
    """Safe, machine-readable failure returned by a provider operation.

    ``message_submitted`` is specific to send operations: ``False`` guarantees
    that the user's prompt was not sent, while ``None`` means the boundary
    cannot be proven. ``code`` is stable for callers; display text is not.
    """

    code = "provider_error"
    message_submitted: bool | None = None


class FeatureUnavailable(ProviderError):
    code = "feature_unavailable"

    def __init__(self, capability, reason: str = "unsupported"):
        super().__init__(f"{capability.value}: {reason}")
        self.capability = capability
        self.reason = reason


class RateLimited(ProviderError):
    code = "rate_limited"

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

    code = "account_paused"


class CompatibilityError(ProviderError):
    code = "capability_changed"

    def __init__(self, capability: Capability):
        super().__init__("provider capability changed")
        self.capability = capability


class RemoteNotFound(ProviderError):
    """The provider confirmed that this remote record no longer exists."""

    code = "remote_not_found"

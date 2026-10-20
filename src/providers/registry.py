"""Provider registration and capability-level health."""

from dataclasses import dataclass, field
from typing import Callable

from .api import Capability, Provider

PROVIDERS = {
    "chatgpt": ("ChatGPT", "◎", "https://chatgpt.com/"),
    "gemini": ("Gemini", "✦", "https://gemini.google.com/"),
    "claude": ("Claude", "✳", "https://claude.ai/"),
}


@dataclass
class _Entry:
    provider: Provider
    account_factory: Callable | None = None
    enabled: bool = True
    unavailable: set[Capability] = field(default_factory=set)


class ProviderRegistry:
    def __init__(self):
        self._providers: dict[str, _Entry] = {}

    def register(self, provider: Provider, *, account_factory: Callable | None = None) -> None:
        if not provider.id or provider.id in self._providers:
            raise ValueError("provider ID must be nonempty and unique")
        self._providers[provider.id] = _Entry(provider, account_factory)

    def create_account(self, provider_id, account_id, **configuration):
        """Construct an independent account adapter using host-supplied configuration."""
        factory = self._providers[provider_id].account_factory
        if factory is None:
            raise ValueError("provider has no account factory")
        provider = factory(account_id, **configuration)
        if provider.id != provider_id or provider.account_id != account_id:
            raise ValueError("account factory returned a different provider or account")
        return provider

    def set_enabled(self, provider_id: str, enabled: bool) -> None:
        """Affects future operations; already running operations are not killed."""
        self._providers[provider_id].enabled = enabled

    def require(self, provider_id: str, capability: Capability) -> Provider:
        entry = self._providers[provider_id]
        if not entry.enabled:
            raise ValueError("provider is disabled")
        if capability not in entry.provider.capabilities:
            raise ValueError("capability is unsupported")
        if capability in entry.unavailable:
            raise ValueError("capability is unavailable")
        return entry.provider

    def mark_unavailable(self, provider_id: str, capability: Capability) -> None:
        self._providers[provider_id].unavailable.add(capability)

    def restore(self, provider_id: str, capability: Capability) -> None:
        """Explicit recovery after an adapter update or user-requested check."""
        self._providers[provider_id].unavailable.discard(capability)

    def get(self, provider_id: str) -> Provider:
        return self._providers[provider_id].provider

    def ids(self) -> tuple[str, ...]:
        return tuple(self._providers)

    async def describe(self, provider_id: str):
        from .models import Feature, FeatureState, ProviderDescription

        entry = self._providers[provider_id]
        describe = getattr(entry.provider, "describe", None)
        if describe:
            result = await describe()
        else:
            result = ProviderDescription(
                provider_id,
                tuple(
                    Feature(
                        cap,
                        FeatureState.AVAILABLE
                        if cap in entry.provider.capabilities
                        else FeatureState.UNSUPPORTED,
                    )
                    for cap in Capability
                ),
            )
        return ProviderDescription(
            result.id,
            tuple(
                Feature(feature.capability, FeatureState.UNAVAILABLE, "disabled")
                if not entry.enabled
                else Feature(feature.capability, FeatureState.UNAVAILABLE, "compatibility_failure")
                if feature.capability in entry.unavailable
                else feature
                for feature in result.features
            ),
        )

"""Shared account identity, provider metadata, and model catalog records."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from typing import Any
from urllib.parse import quote, unquote

from webaiprovider import (
    Capability,
    MemoryCredentialStore,
    ModelRef,
    WebAIAccount,
    WebAIProvider,
    WebAIProviderType,
)
from webaiprovider.resources.model.model import ModelCatalogState

CATALOG_SCHEMA = 3
MODEL_TIMEOUT = 30
HISTORY_TIMEOUT = 20
_LOG = logging.getLogger("accounts")

PROVIDERS: dict[str, tuple[str, str]] = {
    WebAIProviderType.CHATGPT.value: ("ChatGPT", "https://chatgpt.com/"),
    WebAIProviderType.GEMINI.value: ("Gemini", "https://gemini.google.com/"),
    WebAIProviderType.CLAUDE.value: ("Claude", "https://claude.ai/"),
    WebAIProviderType.GROK.value: ("Grok", "https://grok.com/"),
}


@lru_cache(maxsize=None)
def provider_capabilities(provider_id: str) -> frozenset[Capability]:
    """Return declared static capabilities for a provider without opening a session."""
    try:
        ptype = WebAIProviderType.parse(provider_id)
    except ValueError:
        return frozenset()
    provider = WebAIProvider(
        WebAIAccount(id="_capabilities", provider=ptype.value, credentials=MemoryCredentialStore()),
        type=ptype,
    )
    try:
        return frozenset(provider.capabilities)
    finally:
        provider.close()


@dataclass(frozen=True)
class CatalogChoice:
    id: str
    title: str
    is_default: bool = False


@dataclass(frozen=True)
class CatalogModel:
    id: str
    title: str
    efforts: tuple[CatalogChoice, ...] = ()
    default_effort: str | None = None
    chat_title: str | None = None
    is_default: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


def _format_gpt_model_title(model_id: str, title: str) -> str:
    lowered = model_id.casefold()
    if not lowered.startswith("gpt-"):
        return title
    if lowered.endswith("-instant"):
        suffix = "Instant"
    elif lowered.endswith(("-thinking", "-t-mini")):
        suffix = "Thinking"
    else:
        return title
    if title.casefold().endswith(suffix.casefold()):
        return title
    return f"{title} {suffix}"


@dataclass(frozen=True)
class AccountCatalog:
    state: ModelCatalogState
    chat_models: tuple[CatalogModel, ...] = ()
    default_chat_model: CatalogModel | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.default_chat_model is None and self.chat_models:
            default_item = next(
                (item for item in self.chat_models if item.is_default), self.chat_models[0]
            )
            object.__setattr__(self, "default_chat_model", default_item)

    @classmethod
    def from_model_refs(
        cls,
        models: Sequence[ModelRef],
        *,
        state: ModelCatalogState = ModelCatalogState.AVAILABLE,
        reason: str | None = None,
    ) -> "AccountCatalog":
        if state != ModelCatalogState.AVAILABLE and not models:
            return cls(state=state, reason=reason)
        items: list[CatalogModel] = []
        default_item: CatalogModel | None = None
        for model in models:
            default_effort_id = model.default_effort_id
            efforts = tuple(
                CatalogChoice(
                    id=effort.id,
                    title=effort.name or effort.id,
                    is_default=effort.id == default_effort_id,
                )
                for effort in model.efforts.list()
            )
            meta = dict(model.metadata) if isinstance(model.metadata, Mapping) else {}
            is_default = bool(meta.get("default") or meta.get("is_default"))
            item = CatalogModel(
                id=model.id,
                title=_format_gpt_model_title(model.id, model.name or model.id),
                efforts=efforts,
                default_effort=default_effort_id,
                chat_title=meta.get("chat_title")
                if isinstance(meta.get("chat_title"), str)
                else None,
                is_default=is_default,
                metadata=meta,
            )
            items.append(item)
            if is_default and default_item is None:
                default_item = item
        if default_item is None and items:
            default_item = items[0]
        return cls(
            state=state,
            chat_models=tuple(items),
            default_chat_model=default_item,
            reason=reason,
        )

    def chat_model(self, model_id: str | None) -> CatalogModel | None:
        if not model_id:
            return None
        return next((item for item in self.chat_models if item.id == model_id), None)

    def effort(self, model_id: str | None, preferred: str | None = None) -> str | None:
        selected = self.chat_model(model_id) or self.default_chat_model
        if selected is None or not selected.efforts:
            return None
        if selected.default_effort is None and preferred == "medium":
            return None
        if preferred:
            match = next((c.id for c in selected.efforts if c.id == preferred), None)
            if match is not None:
                return match
        return selected.default_effort if selected.default_effort else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "reason": self.reason,
            "chat_models": [asdict(item) for item in self.chat_models],
            "default_model_id": self.default_chat_model.id if self.default_chat_model else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AccountCatalog":
        state = ModelCatalogState(data["state"])
        models: list[CatalogModel] = []
        default_id = data.get("default_model_id")
        default_model: CatalogModel | None = None
        for raw in data.get("chat_models") or ():
            efforts = tuple(
                CatalogChoice(
                    id=str(choice["id"]),
                    title=str(choice.get("title") or choice["id"]),
                    is_default=bool(choice.get("is_default", False)),
                )
                for choice in raw.get("efforts") or ()
            )
            item = CatalogModel(
                id=str(raw["id"]),
                title=str(raw.get("title") or raw["id"]),
                efforts=efforts,
                default_effort=raw.get("default_effort"),
                chat_title=raw.get("chat_title"),
                is_default=bool(raw.get("is_default", False)),
                metadata=dict(raw.get("metadata") or {}),
            )
            models.append(item)
            if (default_id and item.id == default_id) or (default_id is None and item.is_default):
                default_model = item
        if default_model is None and models:
            default_model = models[0]
        return cls(
            state=state,
            chat_models=tuple(models),
            default_chat_model=default_model,
            reason=data.get("reason"),
        )


def identity(context: str | None, remote: str | None) -> str | None:
    return "@" + context + "/" + quote(remote, safe="") if remote and context else None


def origin(value: str | None, fallback: str = "chatgpt/default") -> tuple[str, str | None]:
    if value and value.startswith("@"):
        provider, account, remote = value[1:].split("/", 2)
        return provider + "/" + account, unquote(remote)
    return fallback, value


def _shallow_dataclass(value: Any) -> dict[str, Any]:
    """Project dataclasses when simple flat conversion is needed."""
    from dataclasses import fields

    return {item.name: getattr(value, item.name) for item in fields(value)}

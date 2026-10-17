"""Version-one provider boundary; adapters own all provider-specific details."""

from dataclasses import dataclass
from enum import Enum
from typing import AsyncGenerator, Protocol

from .errors import AccountPaused, CompatibilityError, RateLimited


class Capability(str, Enum):
    CHAT = "chat"
    TEMPORARY = "temporary"
    HISTORY = "history"
    RENAME = "rename"
    DELETE = "delete"
    AUTH = "auth"
    ATTACHMENTS = "attachments"
    RESOURCES = "resources"
    PROJECTS = "projects"
    PROJECT_MEMBERSHIP = "project_membership"
    MODELS = "models"
    EFFORT = "effort"
    CONNECTORS = "connectors"


@dataclass(frozen=True)
class SendRequest:
    provider_id: str
    account_id: str
    chat_id: str  # Stable local identity, including before the remote ID exists.
    prompt: str
    conversation_id: str | None = None
    model: str | None = None
    effort: str | None = None
    project_id: str | None = None
    temporary: bool = False
    attachments: tuple[dict, ...] = ()


@dataclass(frozen=True)
class TextDelta:
    text: str
    channel: str | None = None


@dataclass(frozen=True)
class ReplaceText:
    text: str
    channel: str | None = None


@dataclass(frozen=True)
class ConversationBound:
    conversation_id: str


ProviderEvent = TextDelta | ReplaceText | ConversationBound


class Provider(Protocol):
    id: str
    capabilities: frozenset[Capability]

    def stream(self, request: SendRequest) -> AsyncGenerator[ProviderEvent, None]:
        """Yield events; cancellation must close transport without resending.

        Normal exhaustion means a confirmed successful response. Adapters must
        raise on truncated streams and must never log credentials in exceptions.
        Model/effort identifiers remain provider-owned, not a universal enum.
        """
        ...


__all__ = [
    "AccountPaused",
    "CompatibilityError",
    "RateLimited",
    "Capability",
    "SendRequest",
    "TextDelta",
    "ReplaceText",
    "ConversationBound",
    "ProviderEvent",
    "Provider",
]

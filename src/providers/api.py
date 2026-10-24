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
    PROJECT_CREATE = "project_create"
    PROJECT_UPDATE = "project_update"
    PROJECT_DELETE = "project_delete"
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

    async def prepare(self) -> None:
        """Establish account-scoped transport state before user work begins.

        Preparation must be safe to call in the background and concurrent with
        normal requests. Requests arriving during preparation should share or
        wait for the same initialization rather than opening duplicate sessions.
        """
        ...

    def stream(self, request: SendRequest) -> AsyncGenerator[ProviderEvent, None]:
        """Yield events; cancellation must close transport without resending.

        Normal exhaustion means a confirmed successful response. Adapters must
        raise on truncated streams and must never log credentials in exceptions.
        Model/effort identifiers remain provider-owned, not a universal enum.
        """
        ...


def validate_request(provider, request: SendRequest) -> None:
    """Reject cross-account requests before a transport is opened."""
    if request.provider_id != provider.id or request.account_id != provider.account_id:
        raise ValueError("send request belongs to a different provider/account")


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

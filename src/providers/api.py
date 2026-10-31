"""Version-one provider boundary; adapters own all provider-specific details."""

from dataclasses import dataclass
from enum import Enum
from typing import AsyncGenerator, Protocol

from .errors import AccountPaused, CompatibilityError, ProviderError, RateLimited


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
    MESSAGE_EDIT = "message_edit"
    MESSAGE_RETRY = "message_retry"
    CHAT_BRANCH = "chat_branch"


class MessageAction(str, Enum):
    EDIT = "edit"
    RETRY = "retry"
    BRANCH = "branch"

    @property
    def capability(self):
        return {
            self.EDIT: Capability.MESSAGE_EDIT,
            self.RETRY: Capability.MESSAGE_RETRY,
            self.BRANCH: Capability.CHAT_BRANCH,
        }[self]


@dataclass(frozen=True)
class BranchContext:
    conversation_id: str
    message_id: str


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
    action: MessageAction | None = None
    message_id: str | None = None
    branch: BranchContext | None = None


@dataclass(frozen=True)
class TextDelta:
    text: str
    channel: str | None = None


@dataclass(frozen=True)
class ReplaceText:
    text: str
    channel: str | None = None


@dataclass(frozen=True)
class ResponseComplete:
    """Text generation ended; request confirmation may still be pending."""


@dataclass(frozen=True)
class ConversationBound:
    conversation_id: str


@dataclass(frozen=True)
class MessageMetadata:
    message_id: str | None = None
    user_message_id: str | None = None
    model: str | None = None
    effort: str | None = None
    sources: tuple[dict, ...] = ()
    actions: tuple[str, ...] = ()
    files: tuple[dict, ...] = ()


ProviderEvent = TextDelta | ReplaceText | ResponseComplete | ConversationBound | MessageMetadata


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
        Provider failures derive from ProviderError, expose a stable ``code``,
        and set ``message_submitted`` to False only when the prompt is known not
        to have reached the provider.
        Model/effort identifiers remain provider-owned, not a universal enum.
        """
        ...


def validate_request(provider, request: SendRequest) -> None:
    """Reject cross-account requests before a transport is opened."""
    if request.provider_id != provider.id or request.account_id != provider.account_id:
        raise ValueError("send request belongs to a different provider/account")
    if request.action is not None:
        if request.action not in (MessageAction.EDIT, MessageAction.RETRY):
            raise ValueError("unsupported generation action")
        if not request.conversation_id or not request.message_id:
            raise ValueError("message actions require a conversation and message identity")
    if request.branch is not None:
        if request.action is not None or request.temporary:
            raise ValueError("a branch continuation requires a new persistent turn")
        if not request.branch.conversation_id or not request.branch.message_id:
            raise ValueError("a branch continuation requires its source identities")
        if provider.id != "chatgpt":
            raise ValueError(
                "this provider creates branches immediately; no continuation context is needed"
            )


__all__ = [
    "AccountPaused",
    "CompatibilityError",
    "ProviderError",
    "RateLimited",
    "Capability",
    "MessageAction",
    "MessageMetadata",
    "BranchContext",
    "SendRequest",
    "TextDelta",
    "ReplaceText",
    "ConversationBound",
    "ProviderEvent",
    "Provider",
]

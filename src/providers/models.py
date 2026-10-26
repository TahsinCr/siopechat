"""Provider-neutral records with explicit provenance and lossless provider metadata."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generic, TypeVar

from .api import Capability


class FeatureState(str, Enum):
    AVAILABLE = "available"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"
    LOGIN_REQUIRED = "login_required"


class SessionState(str, Enum):
    UNKNOWN = "unknown"
    LOGIN_REQUIRED = "login_required"
    AUTHENTICATED = "authenticated"


@dataclass(frozen=True)
class SessionStatus:
    state: SessionState
    saved_snapshot: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class Feature:
    capability: Capability
    state: FeatureState
    reason: str | None = None


@dataclass(frozen=True)
class ProviderDescription:
    id: str
    features: tuple[Feature, ...]

    def feature(self, capability: Capability) -> Feature:
        return next(
            (item for item in self.features if item.capability == capability),
            Feature(capability, FeatureState.UNKNOWN),
        )


@dataclass(frozen=True)
class Choice:
    id: str
    title: str


@dataclass(frozen=True)
class Model:
    id: str
    title: str
    efforts: tuple[Choice, ...] = ()
    default_effort: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict, repr=False)
    chat_selectable: bool = True
    chat_title: str | None = None


@dataclass(frozen=True)
class ModelCatalog:
    state: FeatureState
    models: tuple[Model, ...] = ()
    default_model: str | None = None
    reason: str | None = None

    @property
    def chat_models(self) -> tuple[Model, ...]:
        return tuple(model for model in self.models if model.chat_selectable)

    def chat_model(self, model_id: str | None) -> Model | None:
        return next(
            (model for model in self.models if model.chat_selectable and model.id == model_id), None
        )

    @property
    def default_chat_model(self) -> Model | None:
        return next(
            (model for model in self.chat_models if model.id == self.default_model),
            next(iter(self.chat_models), None),
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ModelCatalog":
        return cls(
            FeatureState(value["state"]),
            tuple(
                Model(
                    item["id"],
                    item["title"],
                    tuple(Choice(**effort) for effort in item.get("efforts", [])),
                    item.get("default_effort"),
                    item.get("metadata", {}),
                    item.get("chat_selectable", True),
                    item.get("chat_title"),
                )
                for item in value.get("models", [])
            ),
            value.get("default_model"),
            value.get("reason"),
        )

    def effort(self, model_id: str, preferred: str = "medium") -> str | None:
        model = next((m for m in self.models if m.id == model_id), None)
        if model is None:
            return None
        supported = {e.id for e in model.efforts}
        if preferred in supported:
            return preferred
        return model.default_effort if model.default_effort in supported else None


@dataclass(frozen=True)
class Origin:
    provider_id: str
    account_id: str
    remote_id: str


@dataclass(frozen=True)
class Resource:
    id: str
    name: str
    media_type: str | None = None
    remote_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Content:
    kind: str
    text: str | None = None
    resource: Resource | None = None
    metadata: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Message:
    id: str
    role: str
    content: tuple[Content, ...]
    channel: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict, repr=False)
    actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class Conversation:
    origin: Origin
    title: str
    messages: tuple[Message, ...] = ()
    project_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Project:
    origin: Origin
    name: str
    instructions: str | None = None
    resources: tuple[Resource, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict, repr=False)


T = TypeVar("T")


@dataclass(frozen=True)
class Page(Generic[T]):
    items: tuple[T, ...]
    next_cursor: str | None = None

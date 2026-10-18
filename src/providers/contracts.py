"""Optional common feature contracts; use describe() before offering operations."""

from typing import Protocol

from .api import Capability, Provider
from .errors import FeatureUnavailable
from .models import Conversation, ModelCatalog, Page, Project, ProviderDescription, SessionStatus


class Backend(Provider, Protocol):
    async def login(self) -> SessionStatus: ...
    async def authenticate(self) -> SessionStatus: ...
    async def session_status(self) -> SessionStatus: ...
    async def forget_session(self) -> SessionStatus: ...
    async def describe(self) -> ProviderDescription: ...
    async def models(self) -> ModelCatalog: ...
    async def list_chats(
        self, cursor: str | None = None, limit: int = 28
    ) -> Page[Conversation]: ...
    async def get_chat(self, conversation_id: str) -> Conversation: ...
    async def rename_chat(self, conversation_id: str, title: str) -> None: ...
    async def delete_chat(self, conversation_id: str) -> None: ...
    async def list_projects(self, cursor: str | None = None) -> Page[Project]: ...
    async def get_project(self, project_id: str) -> Project: ...
    async def list_project_chats(
        self, project_id: str, cursor: str | None = None
    ) -> Page[Conversation]: ...
    async def create_project(
        self, name: str, instructions: str = "", *, visibility="private", memory="default"
    ) -> Project: ...
    async def update_project(self, project: Project) -> Project: ...
    async def delete_project(self, project_id: str) -> None: ...
    async def move_chat(self, conversation_id: str, project_id: str | None) -> None: ...


class OptionalFeatures:
    """Explicit failures keep unimplemented operations distinct from empty results."""

    async def delete_chat(self, conversation_id: str) -> None:
        raise FeatureUnavailable(Capability.DELETE, "not_implemented")

    async def list_projects(self, cursor: str | None = None) -> Page[Project]:
        raise FeatureUnavailable(Capability.PROJECTS, "not_implemented")

    async def get_project(self, project_id: str) -> Project:
        raise FeatureUnavailable(Capability.PROJECTS, "not_implemented")

    async def list_project_chats(
        self, project_id: str, cursor: str | None = None
    ) -> Page[Conversation]:
        raise FeatureUnavailable(Capability.PROJECTS, "not_implemented")

    async def create_project(
        self, name: str, instructions: str = "", *, visibility="private", memory="default"
    ) -> Project:
        raise FeatureUnavailable(Capability.PROJECT_CREATE, "not_implemented")

    async def update_project(self, project: Project) -> Project:
        raise FeatureUnavailable(Capability.PROJECTS, "not_implemented")

    async def delete_project(self, project_id: str) -> None:
        raise FeatureUnavailable(Capability.PROJECT_DELETE, "not_implemented")

    async def move_chat(self, conversation_id: str, project_id: str | None) -> None:
        raise FeatureUnavailable(Capability.PROJECT_MEMBERSHIP, "not_implemented")

"""Optional common feature contracts; use describe() before offering operations."""

import asyncio
from typing import Protocol

from .api import Capability, Provider
from .errors import FeatureUnavailable, ProviderError, RemoteNotFound
from .files import DownloadSource
from .models import Conversation, ModelCatalog, Page, Project, ProviderDescription, SessionStatus
from .projects import ProjectOptions


class Backend(Provider, Protocol):
    account_id: str

    async def close(self) -> None: ...
    def attachment_policy(self) -> dict | None: ...
    async def upload_file(self, path) -> dict: ...
    async def login(self) -> SessionStatus: ...
    async def authenticate(self) -> SessionStatus: ...
    async def session_status(self) -> SessionStatus: ...
    async def forget_session(self) -> SessionStatus: ...
    async def describe(self) -> ProviderDescription: ...
    async def models(self, *, force: bool = False) -> ModelCatalog: ...
    async def list_chats(
        self, cursor: str | None = None, limit: int = 28
    ) -> Page[Conversation]: ...
    async def get_chat(self, conversation_id: str) -> Conversation: ...
    async def branch_chat(self, conversation_id: str, message_id: str) -> Conversation: ...
    async def conversation_attachment_count(self, conversation_id: str) -> int | None: ...
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
    async def project_options(self, project_id: str | None = None) -> ProjectOptions: ...
    async def add_project_resource(self, project_id: str, kind: str, **values) -> Project: ...
    async def remove_project_resource(self, project_id: str, resource_id: str) -> Project: ...
    async def delete_project(self, project_id: str) -> None: ...
    async def move_chat(self, conversation_id: str, project_id: str | None) -> None: ...

    async def resolve_download(
        self, url: str, conversation_id: str | None = None, message_id: str | None = None
    ) -> DownloadSource | None: ...


class OptionalFeatures:
    """Explicit failures keep unimplemented operations distinct from empty results."""

    async def branch_chat(self, conversation_id: str, message_id: str) -> Conversation:
        raise FeatureUnavailable(Capability.CHAT_BRANCH)

    async def resolve_download(self, url, conversation_id=None, message_id=None):
        return None

    def attachment_policy(self):
        return None

    async def upload_file(self, path):
        raise FeatureUnavailable(Capability.ATTACHMENTS, "not_implemented")

    async def conversation_attachment_count(self, conversation_id: str) -> int | None:
        """Return the provider's known file count, or None when it is unknown."""
        raise FeatureUnavailable(Capability.ATTACHMENTS, "not_implemented")

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
        raise FeatureUnavailable(Capability.PROJECT_UPDATE, "not_implemented")

    async def project_options(self, project_id=None):
        return ProjectOptions()

    async def add_project_resource(self, project_id, kind, **values):
        raise FeatureUnavailable(Capability.RESOURCES, "not_implemented")

    async def remove_project_resource(self, project_id, resource_id):
        raise FeatureUnavailable(Capability.RESOURCES, "not_implemented")

    async def delete_project(self, project_id: str) -> None:
        raise FeatureUnavailable(Capability.PROJECT_DELETE, "not_implemented")

    async def move_chat(self, conversation_id: str, project_id: str | None) -> None:
        raise FeatureUnavailable(Capability.PROJECT_MEMBERSHIP, "not_implemented")


async def confirm_deleted(read, missing=lambda value: False):
    """Retry only the readback; callers submit the destructive request exactly once."""
    for delay in (0, 0.2, 0.5, 1):
        if delay:
            await asyncio.sleep(delay)
        try:
            value = await read()
        except RemoteNotFound:
            return
        if missing(value):
            return
    raise ProviderError("Silme sunucuda doğrulanamadı; yerel kayıt korundu. Eşitleyin.")

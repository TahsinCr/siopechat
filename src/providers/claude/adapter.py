"""Claude web adapter. Authentication stays inside the selected browser profile."""

import asyncio
from pathlib import Path

from ..api import Capability, ConversationBound, ReplaceText
from ..browser import BrowserTransport
from ..contracts import OptionalFeatures
from ..errors import AccountPaused, FeatureUnavailable, ProviderError
from ..models import (
    Choice,
    Feature,
    FeatureState,
    Model,
    ModelCatalog,
    Origin,
    Page,
    ProviderDescription,
    SessionState,
    SessionStatus,
)
from ..projects import ProjectEditing, ProjectEditor
from .records import conversation, project


class ClaudeProvider(ProjectEditing, OptionalFeatures):
    id = "claude"
    capabilities = frozenset(
        {
            Capability.CHAT,
            Capability.AUTH,
            Capability.HISTORY,
            Capability.MODELS,
            Capability.EFFORT,
            Capability.PROJECTS,
            Capability.PROJECT_CREATE,
            Capability.PROJECT_UPDATE,
            Capability.RESOURCES,
            Capability.PROJECT_DELETE,
            Capability.PROJECT_MEMBERSHIP,
            Capability.DELETE,
            Capability.RENAME,
        }
    )

    def __init__(
        self,
        credential_file,
        account_id="default",
        *,
        cdp_url="http://127.0.0.1:9232",
        ensure_ready=None,
    ):
        self.credential_file = Path(credential_file)
        self.account_id = account_id
        self.browser = BrowserTransport(
            Path(__file__).parent / "scripts" / "browser.cjs", cdp_url, ensure_ready
        )
        self._gate = asyncio.Lock()
        self.editor = ProjectEditor(
            Path(__file__).parent / "scripts" / "projects.cjs", cdp_url, ensure_ready
        )

    async def close(self):
        await self.editor.close()
        await self.browser.close()

    def _origin(self, remote):
        return Origin(self.id, self.account_id, remote)

    async def authenticate(self):
        async with self._gate:
            await self.browser.request("status")
        return SessionStatus(SessionState.AUTHENTICATED, True)

    async def login(self):
        return await self.authenticate()

    async def session_status(self):
        return SessionStatus(
            SessionState.AUTHENTICATED
            if self.browser.authenticated
            else SessionState.UNKNOWN
            if self.credential_file.is_file()
            else SessionState.LOGIN_REQUIRED,
            self.credential_file.is_file(),
        )

    async def forget_session(self):
        async with self._gate:
            await self.close()
            self.credential_file.unlink(missing_ok=True)
            self.browser.authenticated = False
        return SessionStatus(SessionState.LOGIN_REQUIRED)

    async def describe(self):
        return ProviderDescription(
            self.id,
            tuple(
                Feature(
                    c,
                    FeatureState.AVAILABLE if c in self.capabilities else FeatureState.UNSUPPORTED,
                )
                for c in Capability
            ),
        )

    async def models(self):
        async with self._gate:
            data = await self.browser.request("models")
            models = []
            for item in data["models"]:
                efforts = tuple(Choice(x["id"], x["title"]) for x in item["efforts"])
                default = next((x["id"] for x in item["efforts"] if x.get("selected")), None)
                models.append(
                    Model(
                        item["id"],
                        item["title"],
                        efforts,
                        default_effort=default,
                        chat_selectable=not bool(item.get("selection_error")),
                        metadata={
                            k: item[k] for k in ("effort_error", "selection_error") if item.get(k)
                        },
                    )
                )
            return ModelCatalog(FeatureState.AVAILABLE, tuple(models), data.get("default_model"))

    async def list_chats(self, cursor=None, limit=50):
        async with self._gate:
            values = await self.browser.request("chats", cursor=cursor, limit=limit)
            if not isinstance(values, list):
                raise ProviderError("Claude conversation list contract changed")
            return Page(
                tuple(conversation(x, self.account_id) for x in values),
                str(int(cursor or 0) + limit) if len(values) == limit else None,
            )

    async def get_chat(self, conversation_id):
        async with self._gate:
            return conversation(
                await self.browser.request("history", conversation_id=conversation_id),
                self.account_id,
            )

    async def list_projects(self, cursor=None):
        if cursor:
            raise ValueError("Claude project listing does not use a cursor")
        async with self._gate:
            values = await self.browser.request("projects")
            if not isinstance(values, list):
                raise ProviderError("Claude project list contract changed")
            return Page(tuple(project(x, self.account_id) for x in values))

    async def get_project(self, project_id):
        async with self._gate:
            return project(
                await self.browser.request("project", project_id=project_id), self.account_id
            )

    async def list_project_chats(self, project_id, cursor=None):
        if cursor:
            raise ValueError("Claude project conversation listing does not use a cursor")
        async with self._gate:
            values = await self.browser.request("project_chats", project_id=project_id)
            if not isinstance(values, list):
                raise ProviderError("Claude project conversation contract changed")
            return Page(
                tuple(
                    conversation(dict(x, project_uuid=project_id), self.account_id) for x in values
                )
            )

    async def create_project(
        self, name, instructions="", *, visibility="private", memory="default"
    ):
        if visibility != "private" or memory != "default":
            raise FeatureUnavailable(
                Capability.PROJECT_CREATE, "Claude privacy/memory override is not available"
            )
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200:
            raise ValueError("Project name must contain 1–200 characters")
        async with self._gate:
            return project(
                await self.browser.request(
                    "create_project", name=name.strip(), instructions=instructions
                ),
                self.account_id,
            )

    async def rename_chat(self, conversation_id, title):
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 200:
            raise ValueError("Chat title must contain 1–200 characters")
        async with self._gate:
            await self.browser.request(
                "rename_chat", conversation_id=conversation_id, title=title.strip()
            )

    async def delete_chat(self, conversation_id):
        async with self._gate:
            await self.browser.request("delete_chat", conversation_id=conversation_id)

    async def delete_project(self, project_id):
        async with self._gate:
            await self.browser.request("delete_project", project_id=project_id)

    async def move_chat(self, conversation_id, project_id):
        async with self._gate:
            await self.browser.request(
                "move_chat", conversation_id=conversation_id, project_id=project_id
            )

    async def stream(self, request):
        if request.temporary or request.attachments:
            raise FeatureUnavailable(
                Capability.TEMPORARY if request.temporary else Capability.ATTACHMENTS,
                "not_available",
            )
        if not self.credential_file.is_file() and not self.browser.authenticated:
            raise AccountPaused("Claude hesabını Hesaplar bölümünden doğrulayın")
        async with self._gate:
            completed = False
            async for event in self.browser.events(
                "send",
                prompt=request.prompt,
                conversation_id=request.conversation_id,
                project_id=request.project_id,
                model=request.model,
                effort=request.effort,
            ):
                if event["type"] == "bound":
                    yield ConversationBound(event["id"])
                elif event["type"] == "replace":
                    yield ReplaceText(event["text"], event.get("channel", "final"))
                elif event["type"] == "done":
                    completed = True
            if not completed:
                raise ProviderError("Claude yanıtı tamamlanmadı; istek yeniden gönderilmedi")

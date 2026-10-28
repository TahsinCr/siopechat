"""Independent Claude adapter using a saved web session over HTTPS."""

import asyncio

from ..api import (
    Capability,
    ConversationBound,
    MessageMetadata,
    ReplaceText,
    TextDelta,
    validate_request,
)
from ..contracts import OptionalFeatures
from ..errors import AccountPaused, FeatureUnavailable
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
from ..projects import ProjectEditing
from ..session import delete_session, session_exists
from .errors import ClaudeCompatibilityError, ClaudeStreamError
from .records import conversation, conversation_attachment_count, project
from .transport import ClaudeProjectEditor, ClaudeTransport


class ClaudeProvider(ProjectEditing, OptionalFeatures):
    id = "claude"
    capabilities = frozenset(
        {
            Capability.CHAT,
            Capability.TEMPORARY,
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
            Capability.ATTACHMENTS,
            Capability.MESSAGE_EDIT,
            Capability.MESSAGE_RETRY,
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
        self.credential_file = credential_file
        self.account_id = account_id
        self.transport = ClaudeTransport(self.credential_file)
        self._gate = asyncio.Lock()
        self._rename_gate = asyncio.Lock()
        self.editor = ClaudeProjectEditor(self.transport)

    async def close(self):
        async with self._rename_gate:
            try:
                await self.editor.close()
            except BaseException as editor_error:
                try:
                    await self.transport.close()
                except BaseException as close_error:
                    raise editor_error from close_error
                raise
            await self.transport.close()

    async def prepare(self):
        await self.transport.prepare()

    def attachment_policy(self):
        from ..attachments import policy_for

        return policy_for(self.id).as_dict()

    async def upload_file(self, path):
        async with self._gate:
            return await self.transport.upload_file(path)

    def _origin(self, remote):
        return Origin(self.id, self.account_id, remote)

    async def authenticate(self):
        async with self._gate:
            await self.transport.request("status")
        return SessionStatus(SessionState.AUTHENTICATED, True)

    async def login(self):
        return await self.authenticate()

    async def session_status(self):
        return SessionStatus(
            SessionState.AUTHENTICATED
            if self.transport.authenticated
            else SessionState.UNKNOWN
            if session_exists(self.credential_file)
            else SessionState.LOGIN_REQUIRED,
            session_exists(self.credential_file),
        )

    async def resolve_download(self, url, conversation_id=None, message_id=None):
        from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

        from ..files import DownloadSource, NativeDownloadClient, cookie_header
        from ..session import read_session
        from .transport import remote_id

        parsed = urlsplit(url)
        path = unquote(parsed.path)
        direct = (
            parsed.scheme == "https"
            and parsed.netloc == "claude.ai"
            and parsed.path.endswith("/wiggle/download-file")
        )
        if direct:
            path = parse_qs(parsed.query).get("path", [""])[0]
        elif parsed.scheme not in ("", "sandbox", "computer") or not path.startswith(
            "/mnt/user-data/"
        ):
            return None
        if (parsed.netloc and not direct) or any(part in (".", "..") for part in path.split("/")):
            raise ValueError("Desteklenmeyen sohbet dosyası yolu")
        if not conversation_id:
            raise ValueError("Dosyanın sohbet kimliği henüz alınmadı")
        base = await self.transport.base()
        endpoint = base + "/conversations/" + remote_id(conversation_id) + "/wiggle"
        listing = await self.transport.session.request("GET", endpoint + "/list-files?prefix=")
        files = listing.get("files_metadata", []) if isinstance(listing, dict) else []
        if not any(isinstance(item, dict) and item.get("path") == path for item in files):
            raise ValueError("Dosya sohbet kaydında bulunamadı veya süresi doldu")
        saved = read_session(self.credential_file)
        cookies = saved.get("cookies", saved)
        values = {key: cookies.get(key) for key in ("sessionKey", "lastActiveOrg")}
        if not values["sessionKey"]:
            raise AccountPaused("Claude oturumu doğrulanamadı")
        headers = {
            "Cookie": cookie_header(values, ("sessionKey", "lastActiveOrg")),
            "Referer": "https://claude.ai/",
        }
        return DownloadSource(
            "https://claude.ai"
            + endpoint
            + "/download-file?"
            + urlencode({"path": path}, quote_via=quote),
            path.rsplit("/", 1)[-1],
            True,
            headers,
            NativeDownloadClient,
        )

    async def forget_session(self):
        async with self._gate:
            await self.close()
            delete_session(self.credential_file)
            self.transport.authenticated = False
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

    async def models(self, *, force: bool = False):
        async with self._gate:
            data = await self.transport.request("models", force=force)
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
                            k: item[k]
                            for k in ("effort_error", "selection_error", "native", "selections")
                            if item.get(k)
                        },
                    )
                )
            return ModelCatalog(FeatureState.AVAILABLE, tuple(models), data.get("default_model"))

    async def list_chats(self, cursor=None, limit=50):
        values = await self.transport.request("chats", cursor=cursor, limit=limit)
        if not isinstance(values, list):
            raise ClaudeCompatibilityError("Claude conversation list response changed")
        return Page(
            tuple(conversation(x, self.account_id) for x in values),
            str(int(cursor or 0) + limit) if len(values) == limit else None,
        )

    async def get_chat(self, conversation_id):
        raw = await self.transport.request("history", conversation_id=conversation_id)
        return await asyncio.to_thread(
            conversation,
            raw,
            self.account_id,
        )

    async def conversation_attachment_count(self, conversation_id):
        value = await self.transport.request("history", conversation_id=conversation_id)
        return conversation_attachment_count(value)

    async def list_projects(self, cursor=None):
        if cursor:
            raise ValueError("Claude project listing does not use a cursor")
        async with self._gate:
            values = await self.transport.request("projects")
            if not isinstance(values, list):
                raise ClaudeCompatibilityError("Claude project list response changed")
            return Page(tuple(project(x, self.account_id) for x in values))

    async def get_project(self, project_id):
        return project(
            await self.transport.request("project", project_id=project_id), self.account_id
        )

    async def list_project_chats(self, project_id, cursor=None):
        if cursor:
            raise ValueError("Claude project conversation listing does not use a cursor")
        async with self._gate:
            values = await self.transport.request("project_chats", project_id=project_id)
            if not isinstance(values, list):
                raise ClaudeCompatibilityError("Claude project conversation response changed")
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
                await self.transport.request(
                    "create_project", name=name.strip(), instructions=instructions
                ),
                self.account_id,
            )

    async def rename_chat(self, conversation_id, title):
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 200:
            raise ValueError("Chat title must contain 1–200 characters")
        async with self._rename_gate:
            await self.transport.request(
                "rename_chat", conversation_id=conversation_id, title=title.strip()
            )

    async def delete_chat(self, conversation_id):
        async with self._gate:
            await self.transport.request("delete_chat", conversation_id=conversation_id)

    async def delete_project(self, project_id):
        async with self._gate:
            await self.transport.request("delete_project", project_id=project_id)

    async def move_chat(self, conversation_id, project_id):
        async with self._gate:
            await self.transport.request(
                "move_chat", conversation_id=conversation_id, project_id=project_id
            )

    async def stream(self, request):
        validate_request(self, request)
        if request.temporary and request.project_id:
            raise FeatureUnavailable(
                Capability.TEMPORARY,
                "projects_not_supported",
            )
        if not session_exists(self.credential_file) and not self.transport.authenticated:
            raise AccountPaused("Claude hesabını Hesaplar bölümünden doğrulayın")
        async with self._gate:
            completed = False
            async for event in self.transport.events(
                "send",
                prompt=request.prompt,
                conversation_id=request.conversation_id,
                project_id=request.project_id,
                model=request.model,
                effort=request.effort,
                temporary=request.temporary,
                attachments=request.attachments,
                **(
                    {"action": request.action, "message_id": request.message_id}
                    if request.action
                    else {}
                ),
            ):
                if event["type"] == "bound":
                    yield ConversationBound(event["id"])
                elif event["type"] in ("replace", "delta"):
                    event_type = TextDelta if event["type"] == "delta" else ReplaceText
                    yield event_type(event["text"], event.get("channel", "final"))
                elif event["type"] == "done":
                    completed = True
                elif event["type"] == "metadata":
                    yield MessageMetadata(
                        message_id=event.get("message_id"),
                        user_message_id=event.get("user_message_id"),
                        model=event.get("model") or request.model,
                        effort=request.effort,
                        sources=tuple(event.get("sources", ())),
                        actions=() if request.temporary else tuple(event.get("actions", ())),
                        files=tuple(event.get("files", ())),
                    )
            if not completed:
                raise ClaudeStreamError(
                    "Claude response ended before completion; it was not resent"
                )

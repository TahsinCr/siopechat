"""Gemini web provider implementing the shared provider contract."""

import asyncio
import time
from dataclasses import replace
from pathlib import Path

from ..api import (
    Capability,
    ConversationBound,
    MessageAction,
    MessageMetadata,
    ReplaceText,
    SendRequest,
    validate_request,
)
from ..contracts import OptionalFeatures, confirm_deleted
from ..errors import AccountPaused, FeatureUnavailable, ProviderError
from ..messages import files_from, sources_from
from ..models import (
    Feature,
    FeatureState,
    ModelCatalog,
    Origin,
    Page,
    ProviderDescription,
    SessionState,
    SessionStatus,
)
from ..projects import ProjectEditing
from ..session import delete_session, session_exists
from .errors import GeminiCompatibilityError, GeminiError, GeminiStreamError


class GeminiProvider(ProjectEditing, OptionalFeatures):
    id = "gemini"
    capabilities = frozenset(
        {
            Capability.CHAT,
            Capability.HISTORY,
            Capability.DELETE,
            Capability.AUTH,
            Capability.MODELS,
            Capability.EFFORT,
            Capability.ATTACHMENTS,
            Capability.TEMPORARY,
            Capability.MESSAGE_EDIT,
            Capability.MESSAGE_RETRY,
            Capability.CHAT_BRANCH,
            Capability.RENAME,
        }
    )

    def __init__(
        self,
        cookie_file: Path,
        account_id="default",
        client_factory=None,
        *,
        notebooks=None,
    ):
        self._media = {}
        self.cookie_file = cookie_file
        self.account_id = account_id
        self._factory = client_factory
        self._client = None
        self._catalog_at = None
        self._model_gate = asyncio.Lock()
        self._gate = asyncio.Lock()
        self._connect_gate = asyncio.Lock()
        self._temporary = {}
        self.notebooks = notebooks
        from .notebooks import Notebooks
        from .rpc import NotebookEditor, NotebookRPC

        self.rpc = NotebookRPC(self._connect)
        self.notebooks = notebooks or Notebooks(None, account_id)
        self.notebooks.reader = self.rpc.read
        self.editor = NotebookEditor(self.rpc, self.notebooks)
        self.capabilities = self.capabilities | {
            Capability.PROJECTS,
            Capability.PROJECT_CREATE,
            Capability.PROJECT_UPDATE,
            Capability.PROJECT_DELETE,
            Capability.RESOURCES,
        }

    def attachment_policy(self):
        from ..attachments import policy_for

        return policy_for(self.id).as_dict()

    async def upload_file(self, path):
        import mimetypes

        path = Path(path).expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError("Dosya bulunamadı")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        size = path.stat().st_size
        limit = 2 * 1024 * 1024 * 1024 if mime.startswith("video/") else 100 * 1024 * 1024
        if not 0 < size <= limit:
            raise ValueError("Gemini dosya boyutu sınırı aşıldı")
        return {
            "id": path.name,
            "path": str(path),
            "name": path.name,
            "mime_type": mime,
            "size_bytes": size,
            "deferred_upload": True,
        }

    async def _connect(self):
        async with self._connect_gate:
            return await self._connect_once()

    async def _connect_once(self):
        if self._client is not None:
            return self._client
        if not session_exists(self.cookie_file):
            raise AccountPaused("Gemini oturumu yok; Hesaplar bölümünden giriş yapın")
        from .session import GeminiSession

        client = (
            GeminiSession(self.cookie_file)
            if self._factory is None
            else self._factory(self.cookie_file)
        )
        await client.open()
        self._client = client
        return client

    async def close(self):
        async with self._gate:
            async with self._connect_gate:
                await self._close_unlocked()

    async def _close_unlocked(self):
        editor_error = None
        try:
            await self.editor.close()
        except BaseException as error:
            editor_error = error
        client = self._client
        try:
            if client is not None:
                await client.close()
        except BaseException as close_error:
            if editor_error is not None:
                raise editor_error from close_error
            raise
        else:
            if self._client is client:
                self._client = None
            if self._client is None:
                self._catalog_at = None
        if editor_error is not None:
            raise editor_error

    async def session_status(self):
        return SessionStatus(
            SessionState.AUTHENTICATED
            if self._client
            else SessionState.UNKNOWN
            if session_exists(self.cookie_file)
            else SessionState.LOGIN_REQUIRED,
            session_exists(self.cookie_file),
        )

    async def authenticate(self):
        await self._connect()
        return SessionStatus(SessionState.AUTHENTICATED, True)

    async def prepare(self):
        """Open the authenticated HTTP session and discover its initial models."""
        await self.models()

    async def forget_session(self):
        async with self._gate:
            async with self._connect_gate:
                await self._close_unlocked()
                delete_session(self.cookie_file)
                self._temporary.clear()
        return SessionStatus(SessionState.LOGIN_REQUIRED)

    async def describe(self):
        return ProviderDescription(
            self.id,
            tuple(
                Feature(
                    cap,
                    FeatureState.AVAILABLE
                    if cap in self.capabilities
                    else FeatureState.UNSUPPORTED,
                )
                for cap in Capability
            ),
        )

    async def models(self, *, force: bool = False):
        async with self._model_gate:
            client = await self._connect()
            if (
                force
                or not client.models
                or self._catalog_at is None
                or (time.monotonic() - self._catalog_at >= 300)
            ):
                client.models = await client.discover_models()
                self._catalog_at = time.monotonic()
            return ModelCatalog(FeatureState.AVAILABLE, client.models)

    def _origin(self, remote_id):
        return Origin(self.id, self.account_id, remote_id)

    async def list_chats(self, cursor=None, limit=50):
        try:
            offset = int(cursor or 0)
        except (ValueError, TypeError):
            raise ValueError("invalid Gemini conversation cursor") from None
        if offset < 0 or offset > 5000 or not 1 <= limit <= 100:
            raise ValueError("invalid Gemini conversation page")
        async with self._gate:
            await self._connect()
            from .history import conversations

            parts = await self.rpc.call(
                "MaZiqc", [offset + limit + 1, None, [None, None, 1, None, 1]], "/app"
            )
            chats = conversations(parts, self._origin)
            return Page(
                chats[offset : offset + limit],
                str(offset + limit) if len(chats) > offset + limit else None,
            )

    async def get_chat(self, conversation_id):
        from .history import history

        parts = await self.rpc.call(
            "hNvQHb", [conversation_id, 1000, None, 1, [1], [4], None, 1], "/app"
        )
        if len(parts) != 1:
            raise GeminiCompatibilityError("Gemini history response format changed")
        return await asyncio.to_thread(history, parts[0], self._origin, conversation_id)

    async def branch_chat(self, conversation_id, message_id):
        from ..messages import target_message
        from .session import nested

        current = await self.get_chat(conversation_id)
        _, target = target_message(current.messages, message_id, "assistant")
        async with self._gate:
            result = await self.rpc.call(
                "KDNZr", [conversation_id, target.metadata["turn_id"]], "/app"
            )
            remote = nested(result, 0, 0, 0)
            if (
                not isinstance(remote, str)
                or not remote.startswith("c_")
                or remote == conversation_id
            ):
                raise ProviderError("Gemini did not confirm a new conversation")
        chat = await self.get_chat(remote)
        title = nested(result, 0, 0, 1)
        if isinstance(title, str) and title.strip():
            from dataclasses import replace

            chat = replace(
                chat, title=title, metadata={**chat.metadata, "title_unavailable": False}
            )
        return chat

    async def delete_chat(self, conversation_id):
        async with self._gate:
            await self._connect()
            await self.rpc.call("GzXR5e", [conversation_id], "/app", allow_empty=True)
            await confirm_deleted(
                lambda: self.rpc.call(
                    "hNvQHb",
                    [conversation_id, 1, None, 1, [1], [4], None, 1],
                    "/app",
                    allow_empty=True,
                ),
                lambda parts: not parts,
            )

    async def rename_chat(self, conversation_id, title):
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 100:
            raise ValueError("Gemini chat title must contain 1–100 characters")
        client = await self._connect()
        await client.rename_conversation(conversation_id, title.strip())

    async def delete_project(self, project_id):
        from .notebooks import notebook_id

        remote = notebook_id(project_id)
        async with self._gate:
            await self.editor.write("delete", remote)
        for attempt in range(5):
            if not any(x.origin.remote_id == remote for x in (await self.notebooks.list()).items):
                return
            await asyncio.sleep(0.25 * 2**attempt)
        raise GeminiError("Defter silme sunucuda doğrulanamadı; işlem tekrarlanmadı")

    async def list_projects(self, cursor=None):
        if self.notebooks is None:
            raise FeatureUnavailable(Capability.PROJECTS)
        return await self.notebooks.list(cursor)

    async def project_options(self, project_id=None):
        if not hasattr(self, "editor"):
            return await OptionalFeatures.project_options(self, project_id)
        return await ProjectEditing.project_options(self, project_id)

    async def create_project(
        self, name, instructions="", *, visibility="private", memory="default"
    ):
        if not hasattr(self, "editor") or visibility != "private" or memory != "default":
            raise FeatureUnavailable(Capability.PROJECT_CREATE)
        if not name.strip() or instructions:
            raise ValueError("Defter oluştururken yalnız ad girin; talimatları sonra düzenleyin")
        async with self._gate:
            remote = await self.editor.write("create", name=name.strip())
        return await self.get_project(remote)

    async def get_project(self, project_id):
        if self.notebooks is None:
            raise FeatureUnavailable(Capability.PROJECTS)
        return await self.notebooks.get(project_id)

    async def list_project_chats(self, project_id, cursor=None):
        if self.notebooks is None:
            raise FeatureUnavailable(Capability.PROJECTS)
        return await self.notebooks.chats(project_id, cursor)

    stream_idle_timeout = 120
    stream_total_timeout = 600

    async def stream(self, request: SendRequest):
        validate_request(self, request)
        iterator = self._stream(request)
        try:
            async with asyncio.timeout(self.stream_total_timeout):
                async for event in iterator:
                    yield event
        except TimeoutError:
            raise GeminiStreamError("Gemini isteği 10 dakikalık süre sınırını aştı") from None
        finally:
            await iterator.aclose()

    async def _stream(self, request: SendRequest):
        if request.provider_id != self.id or request.account_id != self.account_id:
            raise ValueError("request belongs to another provider/account")
        from .notebooks import notebook_id

        project = notebook_id(request.project_id) if request.project_id else None
        from .media import resolve_image_links
        from .notebooks import field
        from .transport import generate

        async with self._gate:
            client = await self._connect()
            if not client.models:
                await self.models()
            context = None
            action_files = None
            if project:
                notebook = await self.notebooks.get(project)
                value = field(field(notebook.metadata.get("native"), 1), 12)
                if not isinstance(value, list) or not value or value[0] not in (1, 2):
                    raise ProviderError("Gemini defter bağlamı okunamadı")
                context = (project, value)
            metadata = self._temporary.get(request.conversation_id) if request.temporary else None
            if request.temporary and request.conversation_id and metadata is None:
                raise ProviderError("Geçici Gemini bağlamı artık bellekte değil")
            if metadata is None:
                metadata = [
                    request.conversation_id or "",
                    "",
                    "",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    "",
                ]
                if request.conversation_id:
                    parts = await self.rpc.call(
                        "hNvQHb",
                        [
                            request.conversation_id,
                            1000 if request.action else 1,
                            None,
                            1,
                            [1],
                            [4],
                            None,
                            1,
                        ],
                        "/app",
                    )
                    from .session import nested

                    turns = nested(parts[0], 0, default=[])
                    turn = turns[0] if turns else None
                    if request.action:
                        from ..messages import target_message
                        from .history import history

                        current = history(parts[0], self._origin, request.conversation_id)
                        role = "user" if request.action == MessageAction.EDIT else "assistant"
                        _, target = target_message(current.messages, request.message_id, role)
                        if request.action.value not in target.actions:
                            raise ProviderError("This Gemini message action is not available")
                        selected_index = next(
                            i
                            for i, item in enumerate(turns)
                            if nested(item, 0, 1) == target.metadata["turn_id"]
                        )
                        original = turns[selected_index]
                        action_files = nested(original, 2, 0, 3) or []
                        if not isinstance(action_files, list):
                            raise ProviderError("Gemini message attachments cannot be preserved")
                        if request.action == MessageAction.RETRY:
                            request = replace(request, prompt=nested(original, 2, 0, 0) or "")
                        turn = (
                            turns[selected_index + 1] if selected_index + 1 < len(turns) else None
                        )
                    rid = nested(turn, 0, 1)
                    rcid = nested(turn, 3, 0, 0, 0)
                    if turn is not None and (not rid or not rcid):
                        raise GeminiCompatibilityError("Gemini sohbet devam kimlikleri alınamadı")
                    metadata[1:3] = [rid or "", rcid or ""]
            bound, visible, cid = False, False, request.conversation_id
            final_metadata = None
            output_files, published_sources = (), ()
            await client.sync_activity()
            async for chunk in generate(
                client,
                request,
                metadata,
                context,
                files=action_files,
                request_timeout=(15, self.stream_idle_timeout + 10),
            ):
                cid = chunk.get("cid") or cid
                metadata = chunk["metadata"]
                if cid and not bound:
                    yield ConversationBound(cid)
                    bound = True
                if chunk.get("context_only"):
                    continue
                images = chunk["files"]
                output_files = files_from(
                    {"files": output_files},
                    {
                        "files": [
                            {
                                "id": image["alias"],
                                "name": image["name"],
                                "remote_url": image["url"],
                                "media_type": image.get("media_type"),
                            }
                            for image in images
                        ]
                    },
                )
                published_sources = sources_from(
                    {"sources": published_sources}, {"sources": chunk.get("sources", ())}
                )
                visible = visible or bool(chunk["text"] or images)
                if chunk["thoughts"]:
                    yield ReplaceText(chunk["thoughts"], "analysis")
                for image in images:
                    self._media[cid, image["alias"]] = image
                    self._media[cid, image["url"]] = image
                while len(self._media) > 256:
                    self._media.pop(next(iter(self._media)))
                yield ReplaceText(resolve_image_links(chunk["text"], images), "final")
                rid = metadata[1] if len(metadata) > 1 else None
                if rid:
                    final_metadata = MessageMetadata(
                        message_id=rid + ":model",
                        user_message_id=rid + ":user",
                        model=request.model,
                        effort=request.effort,
                        sources=published_sources,
                        actions=() if request.temporary else ("retry", "branch"),
                        files=output_files,
                    )
            if not visible or not cid:
                raise GeminiStreamError("Gemini yanıtı tamamlanmadı; yeniden gönderilmedi")
            if final_metadata:
                yield final_metadata
            if request.temporary:
                self._temporary[cid] = metadata
                while len(self._temporary) > 32:
                    oldest = next(iter(self._temporary))
                    self._temporary.pop(oldest)

    async def resolve_download(self, url, conversation_id=None, message_id=None):
        from ..files import DownloadSource, NativeDownloadClient, cookie_header
        from .media import PLACEHOLDER, valid_media_url

        stable_reference = bool(PLACEHOLDER.fullmatch(url) or url.startswith("gemini-media:"))
        if not stable_reference and not valid_media_url(url):
            return None
        target = self._media.get((conversation_id, url))
        if stable_reference and conversation_id and conversation_id not in self._temporary:
            target = None
            chat = await self.get_chat(conversation_id)
            for message in chat.messages:
                for content in message.content:
                    resource = content.resource
                    if resource and resource.id == url:
                        target = {"url": resource.remote_url, "name": resource.name}
        if target is None and valid_media_url(url):
            target = {"url": url, "name": "image"}
        if not target or not valid_media_url(target["url"]):
            raise ValueError("Dosyanın gerçek adresi sağlayıcıdan alınamadı; sohbeti eşitleyin")
        from urllib.parse import urlsplit

        headers = {"Referer": "https://gemini.google.com/"}
        # Generated media can require the owning account, through the owning HTTP session.
        # Never attach Google cookies to arbitrary user-controlled CDN subdomains.
        host = urlsplit(target["url"]).hostname
        if host in {"lh3.googleusercontent.com", "lh3-gg.googleusercontent.com"}:
            from ..session import read_session

            client = self._client
            values = (
                client.http.cookies.get_dict()
                if client is not None and client.http is not None
                else read_session(self.cookie_file)
            )
            headers["Cookie"] = cookie_header(values, ("__Secure-1PSID", "__Secure-1PSIDTS"))
        return DownloadSource(target["url"], target["name"], True, headers, NativeDownloadClient)

    async def forget_temporary(self, remote_id):
        self._temporary.pop(remote_id, None)

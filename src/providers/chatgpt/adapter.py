"""Async common API over the independent, synchronous ChatGPT web client."""

import asyncio
import concurrent.futures
import threading
from collections import OrderedDict
from typing import Callable

from ..api import (
    Capability,
    ConversationBound,
    ReplaceText,
    SendRequest,
    TextDelta,
    validate_request,
)
from ..contracts import OptionalFeatures, confirm_deleted
from ..errors import AccountPaused, CompatibilityError, RateLimited
from ..models import (
    Content,
    Conversation,
    Feature,
    FeatureState,
    Message,
    ModelCatalog,
    Origin,
    Page,
    Project,
    ProviderDescription,
    Resource,
    SessionState,
    SessionStatus,
)
from ..projects import ProjectEditing
from .client import ChatGPTClient
from .discovery import project_id
from .errors import SentinelCompatibilityError
from .projects import ChatGPTProjectEditor
from .session import BrowserSession


class _Stopped(Exception):
    pass


class ChatGPTProvider(ProjectEditing, OptionalFeatures):
    id = "chatgpt"
    capabilities = frozenset(
        {
            Capability.CHAT,
            Capability.DELETE,
            Capability.PROJECT_DELETE,
            Capability.ATTACHMENTS,
            Capability.TEMPORARY,
            Capability.HISTORY,
            Capability.RENAME,
            Capability.AUTH,
            Capability.PROJECTS,
            Capability.PROJECT_CREATE,
            Capability.PROJECT_UPDATE,
            Capability.RESOURCES,
            Capability.PROJECT_MEMBERSHIP,
            Capability.MODELS,
            Capability.EFFORT,
        }
    )

    def __init__(
        self,
        session: BrowserSession,
        account_id: str = "default",
        client_factory: Callable = ChatGPTClient,
    ):
        self.session = session
        self.account_id = account_id
        self._factory = client_factory
        self._temporary_conversations = OrderedDict()
        self._gate = asyncio.Lock()
        self.editor = ChatGPTProjectEditor(self.session, self._factory)

    def attachment_policy(self):
        from ..attachments import policy_for

        return policy_for(self.id).as_dict()

    async def upload_file(self, path):
        import mimetypes
        from pathlib import Path

        path = Path(path)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

        def upload():
            with self._factory(self.session) as client:
                return client.upload_file(path.name, mime, path)

        return await asyncio.to_thread(upload)

    async def close(self):
        await self.editor.close()

    async def session_status(self) -> SessionStatus:
        saved = self.session.paths.session_file.is_file()
        return SessionStatus(
            SessionState.UNKNOWN if saved else SessionState.LOGIN_REQUIRED,
            saved,
            "snapshot_not_verified" if saved else "no_saved_session",
        )

    async def login(self) -> SessionStatus:
        async with self._gate:
            await asyncio.to_thread(self.session.launch)
        return SessionStatus(
            SessionState.UNKNOWN,
            self.session.paths.session_file.is_file(),
            "complete_login_in_browser_then_authenticate",
        )

    async def authenticate(self, *, capture_browser: bool = True) -> SessionStatus:
        def verify():
            if capture_browser and self.session.is_ready():
                url, headers = self.session.capture_auth()
                self.session.save(url, headers)
            with self._factory(self.session) as client:
                client.verify()

        async with self._gate:
            await asyncio.to_thread(verify)
        return SessionStatus(SessionState.AUTHENTICATED, self.session.paths.session_file.is_file())

    async def forget_session(self) -> SessionStatus:
        async with self._gate:
            await asyncio.to_thread(self.session.paths.session_file.unlink, missing_ok=True)
            self._temporary_conversations.clear()
        return SessionStatus(SessionState.LOGIN_REQUIRED, False, "browser_session_retained")

    async def describe(self) -> ProviderDescription:
        return ProviderDescription(
            self.id,
            tuple(
                Feature(
                    cap,
                    FeatureState.AVAILABLE if cap in self.capabilities else FeatureState.UNKNOWN,
                    None if cap in self.capabilities else "not_discovered",
                )
                for cap in Capability
            ),
        )

    async def models(self) -> ModelCatalog:
        def read():
            with self._factory(self.session) as client:
                return client.model_catalog()

        async with self._gate:
            return await asyncio.to_thread(read)

    def _project(self, raw: dict) -> Project:
        return Project(
            Origin(self.id, self.account_id, raw["id"]),
            raw["name"],
            raw.get("instructions"),
            tuple(
                Resource(
                    str(item.get("file_id") or item.get("id") or ""),
                    str(item.get("name") or ""),
                    item.get("type"),
                    metadata=dict(item),
                )
                for item in raw.get("resources", [])
            ),
            dict(raw.get("metadata") or {}),
        )

    async def delete_chat(self, conversation_id):
        async with self._gate:

            def remove():
                with self._factory(self.session) as client:
                    client.delete_conversation(conversation_id)

            await asyncio.to_thread(remove)

            async def readback():
                def read():
                    with self._factory(self.session) as client:
                        return client.get_conversation(conversation_id)

                return await asyncio.to_thread(read)

            await confirm_deleted(readback, lambda value: value.get("is_visible") is False)

    async def delete_project(self, project_id):
        from urllib.parse import quote

        def remove():
            with self._factory(self.session) as client:
                client.request(
                    "DELETE",
                    "https://chatgpt.com/backend-api/gizmos/" + quote(project_id, safe=""),
                    expect_json=False,
                )

        async with self._gate:
            await asyncio.to_thread(remove)
        await confirm_deleted(lambda: self.get_project(project_id))

    async def list_projects(self, cursor: str | None = None) -> Page[Project]:
        def read():
            with self._factory(self.session) as client:
                return client.list_projects(cursor)

        async with self._gate:
            raw = await asyncio.to_thread(read)
        return Page(tuple(self._project(item) for item in raw["items"]), raw.get("cursor"))

    async def create_project(
        self, name: str, instructions: str = "", *, visibility="private", memory="default"
    ) -> Project:
        def create():
            with self._factory(self.session) as client:
                return client.create_project(
                    name, instructions, visibility=visibility, memory=memory
                )

        async with self._gate:
            return self._project(await asyncio.to_thread(create))

    async def get_project(self, project_id: str) -> Project:
        def read():
            with self._factory(self.session) as client:
                return client.get_project(project_id)

        async with self._gate:
            return self._project(await asyncio.to_thread(read))

    async def list_project_chats(
        self, project_id: str, cursor: str | None = None
    ) -> Page[Conversation]:
        def read():
            with self._factory(self.session) as client:
                return client.list_project_chats(project_id, cursor)

        async with self._gate:
            raw = await asyncio.to_thread(read)
        return Page(tuple(self._conversation(item) for item in raw["items"]), raw.get("cursor"))

    def _conversation(self, raw: dict) -> Conversation:
        remote_id = raw.get("conversation_id") or raw.get("id")
        if not isinstance(remote_id, str) or not remote_id:
            raise ValueError("provider conversation has no remote ID")
        messages = []
        for item in raw.get("messages") or ():
            if not isinstance(item, dict):
                continue
            content = item.get("content") or {}
            from .formats import message_parts

            parts = message_parts(content)
            parts = parts if isinstance(parts, list) else []
            author = item.get("author")
            author = author if isinstance(author, dict) else {}
            blocks = tuple(
                Content("text", text=part)
                if isinstance(part, str)
                else Content("provider", metadata={"value": part})
                for part in parts
            )
            messages.append(
                Message(
                    str(item.get("id", "")),
                    author.get("role", "unknown"),
                    blocks,
                    "analysis"
                    if content.get("content_type")
                    in ("thoughts", "reasoning_recap", "reasoning_summary")
                    else item.get("channel"),
                    dict(item),
                )
            )
        return Conversation(
            Origin(self.id, self.account_id, remote_id),
            raw.get("title") or "Untitled",
            tuple(messages),
            project_id=project_id(raw),
            metadata=dict(raw),
        )

    async def list_chats(self, cursor: str | None = None, limit: int = 28) -> Page[Conversation]:
        offset = int(cursor or "0")
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("invalid conversation page")

        def read():
            with self._factory(self.session) as client:
                return client.list_page(offset, limit)

        async with self._gate:
            raw = await asyncio.to_thread(read)
        items = raw.get("items", [])
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ValueError("invalid conversation page")
        return Page(
            tuple(self._conversation(item) for item in items),
            str(offset + len(items)) if len(items) == limit else None,
        )

    async def get_chat(self, conversation_id: str) -> Conversation:
        def read():
            with self._factory(self.session) as client:
                return client.get_conversation(conversation_id)

        async with self._gate:
            return self._conversation(await asyncio.to_thread(read))

    async def resolve_download(self, url, conversation_id=None, message_id=None):
        from urllib.parse import quote, unquote, urlsplit

        from ..files import DownloadSource

        parsed = urlsplit(url)
        if (
            parsed.scheme == "https"
            and parsed.netloc == "chatgpt.com"
            and parsed.path == "/backend-api/estuary/content"
        ):

            def resolve_estuary():
                with self._factory(self.session) as client:
                    return DownloadSource(url, force_download=True, headers=dict(client.headers))

            return await asyncio.to_thread(resolve_estuary)
        if parsed.scheme != "sandbox":
            return None
        if not conversation_id:
            raise ValueError("Dosyanın sohbet kimliği henüz alınmadı")
        sandbox_path = unquote(parsed.path)
        if not sandbox_path.startswith("/mnt/data/"):
            raise ValueError("Desteklenmeyen sohbet dosyası yolu")

        def resolve():
            with self._factory(self.session) as client:
                chat = client.get_conversation(conversation_id)
                candidates = chat.get("messages", [])
                if not candidates and isinstance(chat.get("mapping"), dict):
                    candidates = [
                        node.get("message")
                        for node in chat["mapping"].values()
                        if isinstance(node, dict)
                    ]
                import json

                matched = [
                    item
                    for item in candidates
                    if isinstance(item, dict)
                    and sandbox_path in json.dumps(item.get("content", {}), ensure_ascii=False)
                ]
                selected = next(
                    (item for item in matched if item.get("id") == message_id),
                    matched[-1] if matched else None,
                )
                if selected is None or not selected.get("id"):
                    raise ValueError("Dosya sohbet kaydında bulunamadı veya süresi doldu")
                endpoint = (
                    "https://chatgpt.com/backend-api/conversation/"
                    + quote(conversation_id, safe="")
                    + "/interpreter/download"
                )
                result = client.request(
                    "GET",
                    endpoint,
                    params={"message_id": selected["id"], "sandbox_path": sandbox_path},
                ).json()
                target = result.get("download_url") if isinstance(result, dict) else None
                if not target or urlsplit(target).scheme != "https":
                    raise ValueError("Sağlayıcı dosya indirme adresi döndürmedi")
                # Estuary URLs remain authenticated ChatGPT endpoints, unlike signed CDN URLs.
                headers = dict(client.headers) if urlsplit(target).netloc == "chatgpt.com" else {}
                return DownloadSource(target, sandbox_path.rsplit("/", 1)[-1], True, headers)

        return await asyncio.to_thread(resolve)

    async def rename_chat(self, conversation_id: str, title: str) -> None:
        if not title.strip():
            raise ValueError("title must be nonempty")

        def rename():
            with self._factory(self.session) as client:
                client.rename_conversation(conversation_id, title.strip())

        async with self._gate:
            await asyncio.to_thread(rename)

    async def move_chat(self, conversation_id: str, project_id: str | None) -> None:
        def move():
            with self._factory(self.session) as client:
                client.move_conversation(conversation_id, project_id)

        async with self._gate:
            await asyncio.to_thread(move)

    async def stream(self, request: SendRequest):
        validate_request(self, request)
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue(maxsize=32)
        stopped = threading.Event()
        holder = []

        def publish(item):
            if stopped.is_set():
                raise _Stopped()
            future = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
            while True:
                try:
                    future.result(timeout=0.1)
                    return
                except concurrent.futures.TimeoutError:
                    if stopped.is_set():
                        future.cancel()
                        raise _Stopped()

        def event(value):
            kind = value.get("type")
            if kind == "delta":
                publish(("event", TextDelta(value.get("text", ""), value.get("channel"))))
            elif kind == "replace":
                publish(("event", ReplaceText(value.get("text", ""), value.get("channel"))))

        def work():
            try:
                with self._factory(self.session) as client:
                    client.temporary_conversations = self._temporary_conversations
                    holder.append(client)
                    if stopped.is_set():
                        return
                    remote_id, completed, previous = client.send_prompt(
                        request.conversation_id,
                        request.prompt,
                        on_event=event,
                        **(
                            {"attachments": list(request.attachments)}
                            if request.attachments
                            else {}
                        ),
                        **({"model": request.model} if request.model is not None else {}),
                        **({"effort": request.effort} if request.effort is not None else {}),
                        **({"project_id": request.project_id} if request.project_id else {}),
                        **({"temporary": True} if request.temporary else {}),
                    )
                    # Native summaries can arrive only in the completion snapshot.
                    # The common API must not drop these provider-visible blocks.
                    from .formats import message_parts, text_from_parts

                    summaries = []
                    previous = set(previous or ())
                    for item in completed.get("messages", []):
                        if item.get("id") in previous or item.get("recipient") not in (None, "all"):
                            continue
                        if (item.get("metadata") or {}).get("is_visually_hidden_from_conversation"):
                            continue
                        if (item.get("author") or {}).get("role") != "assistant":
                            continue
                        content = item.get("content") or {}
                        if item.get("channel") == "analysis" or content.get("content_type") in (
                            "thoughts",
                            "reasoning_recap",
                            "reasoning_summary",
                        ):
                            text = text_from_parts(message_parts(content))
                            if text:
                                summaries.append(text)
                    if summaries:
                        publish(("event", ReplaceText("\n\n".join(summaries), "analysis")))
                    publish(("event", ConversationBound(remote_id)))
            except _Stopped:
                return
            except SentinelCompatibilityError:
                if not stopped.is_set():
                    publish(("error", CompatibilityError(Capability.CHAT)))
            except (RateLimited, AccountPaused) as error:
                if not stopped.is_set():
                    publish(("error", error))
            except Exception:
                if not stopped.is_set():
                    publish(("error", RuntimeError("provider_send_failed")))
            finally:
                if not stopped.is_set():
                    publish(("done", None))

        async with self._gate:
            worker = asyncio.create_task(asyncio.to_thread(work))
            try:
                while True:
                    kind, value = await queue.get()
                    if kind == "done":
                        break
                    if kind == "error":
                        raise value
                    yield value
            finally:
                stopped.set()
                if holder:
                    # Closing local transport cannot guarantee remote generation stops.
                    await asyncio.to_thread(holder[0].close)
                # Keep the account gate until the worker exits; do not overlap an ambiguous send.
                await asyncio.shield(worker)

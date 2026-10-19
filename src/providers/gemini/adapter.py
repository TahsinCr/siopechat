"""Provider-neutral Gemini access over the maintained gemini-webapi transport."""

import asyncio
import inspect
import json
from pathlib import Path

from ..api import Capability, ConversationBound, ReplaceText, SendRequest
from ..contracts import OptionalFeatures, confirm_deleted
from ..errors import AccountPaused, FeatureUnavailable, ProviderError
from ..models import (
    Choice,
    Content,
    Conversation,
    Feature,
    FeatureState,
    Message,
    Model,
    ModelCatalog,
    Origin,
    Page,
    ProviderDescription,
    Resource,
    SessionState,
    SessionStatus,
)
from ..projects import ProjectEditing


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
        }
    )

    def __init__(
        self,
        cookie_file: Path,
        account_id="default",
        client_factory=None,
        *,
        quiet=True,
        notebooks=None,
    ):
        self._media = {}
        self.cookie_file = Path(cookie_file)
        self.account_id = account_id
        self.quiet = quiet
        self._factory = client_factory
        self._client = None
        self._gate = asyncio.Lock()
        self._connect_gate = asyncio.Lock()
        self._saved_cookies = None
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

    async def _connect(self):
        async with self._connect_gate:
            return await self._connect_once()

    async def _connect_once(self):
        if self._client is not None:
            if getattr(self._client, "_running", True) is not False:
                return self._client
            await self.close()
        if not self.cookie_file.is_file():
            raise AccountPaused("Gemini oturumu yok; Hesaplar bölümünden giriş yapın")
        values = json.loads(self.cookie_file.read_text(encoding="utf-8"))
        if not isinstance(values, dict) or not values.get("__Secure-1PSID"):
            raise AccountPaused("Gemini oturum çerezleri eksik")
        factory = self._factory
        if factory is None:
            try:
                from gemini_webapi import GeminiClient
            except ImportError:
                raise ProviderError(
                    "Gemini için Python 3.11+ ve gemini-webapi bağımlılığı gerekir"
                ) from None
            if self.quiet:
                # verbose=False does not cover quota/init/background SDK logs.
                # Keep other application loggers and their handlers untouched.
                from gemini_webapi.utils.logger import logger

                logger.disable("gemini_webapi")
            from .session import owned_client_type
            from .transport import notebook_client_type

            factory = notebook_client_type(owned_client_type(GeminiClient, self.cookie_file))
        client = factory(values["__Secure-1PSID"], values.get("__Secure-1PSIDTS"))
        try:
            await client.init(timeout=45, auto_close=False, auto_refresh=True, verbose=False)
        except asyncio.CancelledError:
            await client.close()
            raise
        except (AccountPaused, ProviderError):
            await client.close()
            raise
        except Exception as error:
            await client.close()
            raise ProviderError(
                "Gemini oturum bağlantısı kurulamadı (" + type(error).__name__ + ")"
            ) from None
        status = getattr(client, "account_status", None)
        if getattr(status, "name", None) == "UNAUTHENTICATED" or status == 1016:
            await client.close()
            raise AccountPaused(
                "Gemini oturumu sona ermiş; misafir oturumunda gönderim yapılmadı. "
                "Hesaplar → Bağlı tarayıcı ile giriş yapıp oturumu doğrulayın."
            )
        self._client = client
        self._saved_cookies = values
        return client

    async def close(self):
        if hasattr(self, "editor"):
            await self.editor.close()
        client, self._client = self._client, None
        if client is not None:
            await client.close()

    async def session_status(self):
        return SessionStatus(
            SessionState.AUTHENTICATED
            if self._client
            else SessionState.UNKNOWN
            if self.cookie_file.is_file()
            else SessionState.LOGIN_REQUIRED,
            self.cookie_file.is_file(),
        )

    async def authenticate(self):
        await self._connect()
        return SessionStatus(SessionState.AUTHENTICATED, True)

    async def forget_session(self):
        async with self._gate:
            await self.close()
            self.cookie_file.unlink(missing_ok=True)
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

    async def models(self):
        client = await self._connect()
        # The transport exposes a boolean extended_thinking option, not
        # OpenAI-style low/medium/high levels. Never invent those levels.
        extended = (
            "extended_thinking" in inspect.signature(client.generate_content_stream).parameters
        )
        efforts = (
            (Choice("standard", "Standart"), Choice("extended", "Genişletilmiş"))
            if extended
            else ()
        )
        models = tuple(
            Model(
                model.model_name,
                model.display_name or model.model_name,
                efforts,
                metadata={
                    "model_id": model.model_id,
                    "description": model.description,
                    "effort_source": "extended_thinking" if extended else None,
                },
            )
            for model in client.list_models() or []
            if model.is_available
        )
        return ModelCatalog(FeatureState.AVAILABLE, models)

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
            client = await self._connect()
            if hasattr(client, "_fetch_recent_chats"):
                await client._fetch_recent_chats(recent=offset + limit + 1)
            chats = client.list_chats()
            if chats is None:
                raise ProviderError("Gemini sohbet listesi sunucudan alınamadı")
            return Page(
                tuple(
                    Conversation(
                        self._origin(item.cid),
                        item.title or "Gemini sohbeti",
                        metadata={"updated_at": item.timestamp},
                    )
                    for item in chats[offset : offset + limit]
                ),
                str(offset + limit) if len(chats) > offset + limit else None,
            )

    async def get_chat(self, conversation_id):
        if self._factory is None:
            from .history import history

            parts = await self.rpc.call(
                "hNvQHb", [conversation_id, 1000, None, 1, [1], [4], None, 1], "/app"
            )
            if len(parts) != 1:
                raise ProviderError("Gemini geçmiş yanıtı değişmiş")
            return history(parts[0], self._origin, conversation_id)
        async with self._gate:
            client = await self._connect()
            history = await client.read_chat(conversation_id, limit=1000)
            if history is None:
                raise ProviderError("Gemini sohbet geçmişi alınamadı")
            messages = []
            turns = list(reversed(history.turns))
            for i, turn in enumerate(turns):
                output = turn.model_output
                paired = output or (turns[i + 1].model_output if i + 1 < len(turns) else None)
                metadata = getattr(paired, "metadata", None)
                rid = metadata[1] if isinstance(metadata, list) and len(metadata) > 1 else None
                if not isinstance(rid, str) or not rid:
                    import hashlib

                    rid = hashlib.sha256((turn.role + "\0" + turn.text).encode()).hexdigest()
                stable_id = rid + ":" + turn.role
                if output and output.thoughts:
                    messages.append(
                        Message(
                            stable_id + ":thoughts",
                            "assistant",
                            (Content("text", output.thoughts),),
                            channel="analysis",
                        )
                    )
                from .media import render_images, sdk_images

                images = sdk_images(output) if output else []
                messages.append(
                    Message(
                        stable_id,
                        "user" if turn.role == "user" else "assistant",
                        (Content("text", render_images(turn.text, images)),)
                        + tuple(
                            Content(
                                "image",
                                resource=Resource(
                                    image["alias"],
                                    image["name"],
                                    image.get("media_type"),
                                    image["url"],
                                ),
                            )
                            for image in images
                        ),
                    )
                )
            title = next(
                (item.title for item in client.list_chats() or [] if item.cid == conversation_id),
                "Gemini sohbeti",
            )
            return Conversation(
                self._origin(conversation_id),
                title,
                tuple(messages),
                metadata={"history_partial": len(history.turns) >= 1000},
            )

    async def delete_chat(self, conversation_id):
        async with self._gate:
            client = await self._connect()
            if self._factory is not None:
                await client.delete_chat(conversation_id)
                return
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

    async def delete_project(self, project_id):
        from .notebooks import notebook_id

        remote = notebook_id(project_id)
        async with self._gate:
            await self.editor.write("delete", remote)
        for attempt in range(5):
            if not any(x.origin.remote_id == remote for x in (await self.notebooks.list()).items):
                return
            await asyncio.sleep(0.25 * 2**attempt)
        raise ProviderError("Defter silme sunucuda doğrulanamadı; işlem tekrarlanmadı")

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
        iterator = self._stream(request)
        deadline = asyncio.get_running_loop().time() + self.stream_total_timeout
        try:
            while True:
                try:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise ProviderError("Gemini isteği 10 dakikalık süre sınırını aştı")
                    event = await asyncio.wait_for(
                        anext(iterator), timeout=min(self.stream_idle_timeout, remaining)
                    )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise ProviderError(
                            "Gemini isteği 10 dakikalık süre sınırını aştı"
                        ) from None
                    raise ProviderError(
                        "Gemini 120 saniye boyunca veri döndürmedi; istek durduruldu. "
                        "Yeniden göndermeden önce sohbet geçmişini eşitleyin."
                    ) from None
                yield event
        finally:
            await iterator.aclose()

    async def _stream(self, request: SendRequest):
        if request.provider_id != self.id or request.account_id != self.account_id:
            raise ValueError("request belongs to another provider/account")
        from .notebooks import notebook_id

        project = notebook_id(request.project_id) if request.project_id else None
        if request.effort not in (None, "", "standard", "extended"):
            raise ValueError("unsupported Gemini thinking option")
        from contextlib import nullcontext

        from .notebooks import field
        from .transport import notebook_transport

        async with self._gate:
            client = await self._connect()
            context = None
            if project:
                if self.notebooks is None:
                    raise ProviderError("Gemini defter bağlantısı yapılandırılmamış")
                notebook = await self.notebooks.get(project)
                context = field(field(notebook.metadata.get("native"), 1), 12)
                if not isinstance(context, list) or not context or context[0] not in (1, 2):
                    raise ProviderError("Gemini defter bağlamı okunamadı; verileri yenileyin")
            model = client.resolve_model(request.model) if request.model else None
            metadata = self._temporary.get(request.conversation_id) if request.temporary else None
            if request.temporary and request.conversation_id and metadata is None:
                raise ProviderError("Geçici Gemini bağlamı artık bellekte değil")
            chat = client.start_chat(
                metadata=metadata,
                cid=request.conversation_id or "",
                model=model,
            )
            bound = False
            try:
                with notebook_transport(client, project, context) if project else nullcontext():
                    async for chunk in chat.send_message_stream(
                        request.prompt,
                        files=[item["path"] for item in request.attachments],
                        temporary=request.temporary,
                        extended_thinking=request.effort == "extended",
                        current_retry=0,
                    ):
                        if chat.cid and not bound:
                            yield ConversationBound(chat.cid)
                            bound = True
                        if chunk.thoughts:
                            yield ReplaceText(chunk.thoughts, "analysis")
                        from .media import render_images, sdk_images

                        images = sdk_images(chunk)
                        for image in images:
                            self._media[chat.cid, image["alias"]] = image
                            self._media[chat.cid, image["url"]] = image
                        while len(self._media) > 256:
                            self._media.pop(next(iter(self._media)))
                        yield ReplaceText(render_images(chunk.text or "", images), "final")
            except asyncio.CancelledError:
                raise
            except ProviderError:
                raise
            except Exception as error:
                raise ProviderError(
                    "Gemini yanıtı tamamlanamadı ("
                    + type(error).__name__
                    + "); hesabı veya bağlantıyı kontrol edin"
                ) from error
            if not chat.cid:
                raise ProviderError("Gemini sohbet kimliği dönmedi")
            if request.temporary:
                self._temporary[chat.cid] = chat.metadata
                while len(self._temporary) > 32:
                    self._temporary.pop(next(iter(self._temporary)))
            if not bound:
                yield ConversationBound(chat.cid)

    async def resolve_download(self, url, conversation_id=None, message_id=None):
        from ..files import DownloadSource, NativeDownloadClient, cookie_header
        from .media import PLACEHOLDER, valid_media_url

        if not PLACEHOLDER.fullmatch(url) and not valid_media_url(url):
            return None
        target = self._media.get((conversation_id, url))
        if target is None and PLACEHOLDER.fullmatch(url) and conversation_id:
            chat = await self.get_chat(conversation_id)
            for message in chat.messages:
                for content in message.content:
                    resource = content.resource
                    if resource and resource.id == url:
                        target = {"url": resource.remote_url, "name": resource.name}
        if target is None and valid_media_url(url):
            target = {"url": url, "name": "image"}
        if not target or not valid_media_url(target["url"]):
            raise ValueError(
                "Görselin gerçek dosya adresi sağlayıcıdan alınamadı; sohbeti eşitleyin"
            )
        from urllib.parse import urlsplit

        headers = {"Referer": "https://gemini.google.com/"}
        # Generated media can require the owning account, just like SDK Image.save().
        # Never attach Google cookies to arbitrary user-controlled CDN subdomains.
        host = urlsplit(target["url"]).hostname
        if host in {"lh3.googleusercontent.com", "lh3-gg.googleusercontent.com"}:
            from ..session import read_session

            values = self._saved_cookies or read_session(self.cookie_file)
            client = self._client
            if client is not None and hasattr(getattr(client, "cookies", None), "get_dict"):
                values = client.cookies.get_dict()
            headers["Cookie"] = cookie_header(values, ("__Secure-1PSID", "__Secure-1PSIDTS"))
        return DownloadSource(target["url"], target["name"], True, headers, NativeDownloadClient)

    async def forget_temporary(self, remote_id):
        self._temporary.pop(remote_id, None)

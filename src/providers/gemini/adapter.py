"""Provider-neutral Gemini access over the maintained gemini-webapi transport."""

import asyncio
import inspect
import json
from pathlib import Path

from ..api import Capability, ConversationBound, ReplaceText, SendRequest
from ..contracts import OptionalFeatures
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
    SessionState,
    SessionStatus,
)


class GeminiProvider(OptionalFeatures):
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
        self.cookie_file = Path(cookie_file)
        self.account_id = account_id
        self.quiet = quiet
        self._factory = client_factory
        self._client = None
        self._gate = asyncio.Lock()
        self._temporary = {}
        self.notebooks = notebooks
        from .browser import NativeBrowser

        self.browser = (
            NativeBrowser(notebooks.cdp_url, ensure_ready=getattr(notebooks, "ensure_ready", None))
            if isinstance(getattr(notebooks, "cdp_url", None), str)
            else None
        )
        if notebooks is not None:
            self.capabilities = self.capabilities | {Capability.PROJECTS, Capability.PROJECT_DELETE}

    async def _connect(self):
        if self._client is not None:
            return self._client
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
            from .transport import notebook_client_type

            factory = notebook_client_type(GeminiClient)
        client = factory(values["__Secure-1PSID"], values.get("__Secure-1PSIDTS"))
        try:
            await client.init(timeout=45, auto_close=False, auto_refresh=False, verbose=False)
        except asyncio.CancelledError:
            await client.close()
            raise
        except Exception:
            await client.close()
            raise AccountPaused(
                "Gemini oturumu doğrulanamadı; bağlı tarayıcıdan tekrar doğrulayın"
            ) from None
        status = getattr(client, "account_status", None)
        if getattr(status, "name", None) == "UNAUTHENTICATED" or status == 1016:
            await client.close()
            raise AccountPaused(
                "Gemini oturumu sona ermiş; misafir oturumunda gönderim yapılmadı. "
                "Hesaplar → Bağlı tarayıcı ile giriş yapıp oturumu doğrulayın."
            )
        self._client = client
        return client

    async def close(self):
        if self.browser:
            await self.browser.close()
        client, self._client = self._client, None
        if client is not None:
            await client.close()

    def _require_native_session(self):
        if not self.cookie_file.is_file() and not self.browser.authenticated:
            raise AccountPaused("Gemini hesabını Hesaplar bölümünden doğrulayın")

    async def session_status(self):
        return SessionStatus(
            SessionState.AUTHENTICATED
            if self._client or (self.browser and self.browser.authenticated)
            else SessionState.UNKNOWN
            if self.cookie_file.is_file()
            else SessionState.LOGIN_REQUIRED,
            self.cookie_file.is_file(),
        )

    async def authenticate(self):
        async with self._gate:
            if self.browser:
                await self.browser.models()
            else:
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
        async with self._gate:
            if self.browser:
                self._require_native_session()
                return await self.browser.models()
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
                        metadata={"timestamp": item.timestamp},
                    )
                    for item in chats[offset : offset + limit]
                ),
                str(offset + limit) if len(chats) > offset + limit else None,
            )

    async def get_chat(self, conversation_id):
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
                messages.append(
                    Message(
                        stable_id,
                        "user" if turn.role == "user" else "assistant",
                        (Content("text", turn.text),),
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
            if self.browser:
                self._require_native_session()
                await self.browser.delete_chat(conversation_id)
            else:
                client = await self._connect()
                await client.delete_chat(conversation_id)

    async def delete_project(self, project_id):
        from ..management import delete_project
        from .notebooks import notebook_id

        project_id = notebook_id(project_id)
        if not self.browser:
            return await super().delete_project(project_id)
        self._require_native_session()
        async with self._gate:
            if self.browser.ensure_ready:
                await asyncio.to_thread(self.browser.ensure_ready)
            project = await self.notebooks.get(project_id)
            failure = None
            try:
                await delete_project(self.browser.cdp_url, self.id, project_id, name=project.name)
            except ProviderError as error:
                failure = error
            for attempt in range(5):
                if not any(
                    x.origin.remote_id == project_id for x in (await self.notebooks.list()).items
                ):
                    return
                if attempt < 4:
                    await asyncio.sleep(0.25 * 2**attempt)
            if failure:
                raise failure
            raise ProviderError(
                "Defter silme sunucuda doğrulanamadı; yeniden silmeden kontrol edin"
            )

    async def list_projects(self, cursor=None):
        if self.notebooks is None:
            raise FeatureUnavailable(Capability.PROJECTS)
        return await self.notebooks.list(cursor)

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
                        "Yeniden göndermeden önce bağlı tarayıcıdaki sohbeti kontrol edin."
                    ) from None
                yield event
        finally:
            await iterator.aclose()

    async def _stream(self, request: SendRequest):
        if request.provider_id != self.id or request.account_id != self.account_id:
            raise ValueError("request belongs to another provider/account")
        from .notebooks import notebook_id

        project = notebook_id(request.project_id) if request.project_id else None
        if self.browser:
            self._require_native_session()
            async with self._gate:
                from dataclasses import replace

                async for event in self.browser.stream(replace(request, project_id=project)):
                    yield event
            return
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
                    raise ProviderError("Gemini defteri için bağlı tarayıcı gerekli")
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
            try:
                with notebook_transport(client, project, context) if project else nullcontext():
                    async for chunk in chat.send_message_stream(
                        request.prompt,
                        files=[item["path"] for item in request.attachments],
                        temporary=request.temporary,
                        extended_thinking=request.effort == "extended",
                    ):
                        if chunk.thoughts:
                            yield ReplaceText(chunk.thoughts, "analysis")
                        yield ReplaceText(chunk.text or "", "final")
            except asyncio.CancelledError:
                raise
            except ProviderError:
                raise
            except Exception:
                raise ProviderError(
                    "Gemini yanıtı tamamlanamadı; hesabı veya bağlantıyı kontrol edin"
                ) from None
            if not chat.cid:
                raise ProviderError("Gemini sohbet kimliği dönmedi")
            if request.temporary:
                self._temporary[chat.cid] = chat.metadata
                while len(self._temporary) > 32:
                    self._temporary.pop(next(iter(self._temporary)))
            yield ConversationBound(chat.cid)

    async def forget_temporary(self, remote_id):
        self._temporary.pop(remote_id, None)
        if self.browser:
            await self.browser.close_temporary(remote_id)

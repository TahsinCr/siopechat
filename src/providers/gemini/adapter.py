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
        if notebooks is not None:
            self.capabilities = self.capabilities | {Capability.PROJECTS}

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
            factory = GeminiClient
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
        self._client = client
        return client

    async def close(self):
        if self._client is not None:
            await self._client.close()
            self._client = None

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
        async with self._gate:
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
        if cursor:
            return Page(())
        async with self._gate:
            client = await self._connect()
            chats = client.list_chats() or []
            return Page(
                tuple(
                    Conversation(
                        self._origin(item.cid),
                        item.title or "Gemini sohbeti",
                        metadata={"timestamp": item.timestamp},
                    )
                    for item in chats[:limit]
                )
            )

    async def get_chat(self, conversation_id):
        async with self._gate:
            client = await self._connect()
            history = await client.read_chat(conversation_id, limit=1000)
            if history is None:
                raise ProviderError("Gemini sohbet geçmişi alınamadı")
            messages = []
            for i, turn in enumerate(reversed(history.turns)):
                output = turn.model_output
                if output and output.thoughts:
                    messages.append(
                        Message(
                            f"{i}:thoughts",
                            "assistant",
                            (Content("text", output.thoughts),),
                            channel="analysis",
                        )
                    )
                messages.append(
                    Message(
                        str(i),
                        "user" if turn.role == "user" else "assistant",
                        (Content("text", turn.text),),
                    )
                )
            title = next(
                (item.title for item in client.list_chats() or [] if item.cid == conversation_id),
                "Gemini sohbeti",
            )
            return Conversation(self._origin(conversation_id), title, tuple(messages))

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

    async def stream(self, request: SendRequest):
        if request.provider_id != self.id or request.account_id != self.account_id:
            raise ValueError("request belongs to another provider/account")
        from .notebooks import notebook_id

        project = notebook_id(request.project_id) if request.project_id else None
        if request.effort not in (None, "", "standard", "extended"):
            raise ValueError("unsupported Gemini thinking option")
        async with self._gate:
            client = await self._connect()
            model = client.resolve_model(request.model) if request.model else None
            metadata = self._temporary.get(request.conversation_id) if request.temporary else None
            if request.temporary and request.conversation_id and metadata is None:
                raise ProviderError("Geçici Gemini bağlamı artık bellekte değil")
            chat = client.start_chat(
                metadata=metadata,
                cid=request.conversation_id or "",
                model=model,
                **({"gem": project} if project else {}),
            )
            try:
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

    def forget_temporary(self, remote_id):
        self._temporary.pop(remote_id, None)

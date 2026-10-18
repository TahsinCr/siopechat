"""Gemini's native browser composer, independent of the application frontend."""

import asyncio
import json
import uuid
from pathlib import Path

from ..api import ConversationBound, ReplaceText
from ..errors import ProviderError
from ..models import Choice, FeatureState, Model, ModelCatalog


class NativeBrowser:
    def __init__(self, cdp_url, *, ensure_ready=None):
        self.cdp_url = cdp_url
        self.ensure_ready = ensure_ready
        self.tab_id = None
        self.authenticated = False
        self.temporary_tabs = {}
        self.owned_tabs = set()

    async def events(self, mode, *, remember=True, tab_id=None, **options):
        if mode != "close" and self.ensure_ready is not None:
            await asyncio.to_thread(self.ensure_ready)
        script = Path(__file__).parent / "scripts" / "browser.cjs"
        process = await asyncio.create_subprocess_exec(
            "node",
            str(script),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=8 * 1024 * 1024,
        )
        payload = dict(
            cdp_url=self.cdp_url, tab_id=self.tab_id if remember else tab_id, mode=mode, **options
        )
        try:
            process.stdin.write(json.dumps(payload).encode())
            await process.stdin.drain()
            process.stdin.close()
            while True:
                line = await asyncio.wait_for(process.stdout.readline(), 120)
                if not line:
                    break
                event = json.loads(line)
                if event.get("type") == "tab":
                    self.owned_tabs.add(event["id"])
                    if remember:
                        self.tab_id = event["id"]
                    else:
                        yield event
                elif event.get("type") == "error":
                    self.authenticated = False
                    message = event.get("message") or "Gemini browser operation failed"
                    if message == "fetch failed":
                        message = "Gemini bağlı tarayıcı bağlantısı kesildi; mesaj otomatik tekrar gönderilmedi"
                    raise ProviderError(message)
                else:
                    if event.get("type") in ("models", "status", "bound"):
                        self.authenticated = True
                    yield event
            if await process.wait():
                raise ProviderError("Gemini bağlı tarayıcı işlemi tamamlanamadı")
        except asyncio.TimeoutError:
            raise ProviderError(
                "Gemini tarayıcısından veri gelmedi; oturumu kontrol edin"
            ) from None
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()

    async def read(self, mode, **options):
        async for event in self.events(mode, **options):
            if event.get("type") == mode:
                self.authenticated = True
                return event["data"]
        raise ProviderError("Gemini veri okuma işlemi tamamlanmadı")

    async def delete_chat(self, conversation_id):
        confirmed = False
        async for event in self.events("delete_chat", conversation_id=conversation_id):
            if event.get("type") == "deleted" and event.get("id") == conversation_id:
                confirmed = True
                self.authenticated = True
        if not confirmed:
            raise ProviderError("Gemini sohbet silme işlemi doğrulanamadı; listeyi yenileyin")

    async def models(self):
        async for event in self.events("models"):
            if event["type"] != "models":
                continue
            return ModelCatalog(
                FeatureState.AVAILABLE,
                tuple(
                    Model(
                        item["id"],
                        item["title"],
                        tuple(Choice(c["id"], c["title"]) for c in item["efforts"]),
                        default_effort=next(
                            (c["id"] for c in item["efforts"] if c["id"] == "native:4"), None
                        ),
                        metadata={
                            "model_id": item["remote"],
                            "transport": "browser",
                            "effort_source": "native_menu",
                        },
                    )
                    for item in event["items"]
                ),
            )
        raise ProviderError("Gemini model menüsü okunamadı")

    async def stream(self, request):
        complete = False
        temporary_id = None
        tab_id = None
        if request.temporary:
            if not request.conversation_id and len(self.temporary_tabs) >= 32:
                raise ProviderError(
                    "Önce kullanılmayan geçici sohbetlerden birini kapatın (sınır: 32)"
                )
            temporary_id = request.conversation_id or "temporary_" + uuid.uuid4().hex
            tab_id = self.temporary_tabs.get(temporary_id)
            if request.conversation_id and not tab_id:
                raise ProviderError("Geçici Gemini bağlamı artık bellekte değil")
        async for event in self.events(
            "send",
            remember=not request.temporary,
            tab_id=tab_id,
            temporary=request.temporary,
            temporary_id=temporary_id,
            resume_temporary=bool(tab_id),
            prompt=request.prompt,
            model=request.model,
            effort=request.effort,
            project_id=request.project_id,
            conversation_id=None if request.temporary else request.conversation_id,
            attachments=[str(Path(item["path"]).resolve()) for item in request.attachments],
        ):
            if event["type"] == "tab" and request.temporary:
                self.temporary_tabs[temporary_id] = event["id"]
            elif event["type"] == "replace":
                yield ReplaceText(event["text"], event["channel"])
            elif event["type"] == "bound":
                yield ConversationBound(event["id"])
            elif event["type"] == "done":
                complete = True
        if not complete:
            raise ProviderError("Gemini yanıtı tamamlanmadan tarayıcı akışı kapandı")

    async def close_tab(self, tab):
        try:
            async for _ in self.events("close", remember=False, tab_id=tab):
                pass
        except (OSError, ProviderError):
            pass
        finally:
            self.owned_tabs.discard(tab)

    async def close_temporary(self, remote_id):
        tab = self.temporary_tabs.pop(remote_id, None)
        if tab:
            await self.close_tab(tab)

    async def close(self):
        for tab in tuple(self.owned_tabs):
            await self.close_tab(tab)
        self.tab_id = None
        self.temporary_tabs.clear()
        self.owned_tabs.clear()
        self.authenticated = False

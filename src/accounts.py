"""Account-scoped provider orchestration and provenance for the terminal host."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import subprocess
import threading
import uuid
import webbrowser
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote, unquote

from backend import BackendService
from providers import Capability, ConversationBound, ReplaceText, SendRequest, TextDelta
from providers.chatgpt import BrowserSession, ChatGPTClient, ChatGPTProvider, SessionPaths
from providers.gemini import GeminiProvider
from providers.models import SessionState

PROVIDERS = {
    "chatgpt": ("ChatGPT", "◈", "https://chatgpt.com/"),
    "gemini": ("Gemini", "✦", "https://gemini.google.com/"),
}


def identity(context, remote):
    return "@" + context + "/" + quote(remote, safe="") if remote else None


def origin(value, fallback="chatgpt/default"):
    if value and value.startswith("@"):
        provider, account, remote = value[1:].split("/", 2)
        return provider + "/" + account, unquote(remote)
    return fallback, value


class Accounts:
    def __init__(self, api):
        self.api = api
        self.root = api.get("paths")
        self._providers = {}
        self._verified = set()
        self._loop = None
        self._thread = None
        self._lock = threading.Lock()
        cache = self.root / "data" / "gemini-cookie-cache"
        cache.mkdir(parents=True, mode=0o700, exist_ok=True)
        if os.name != "nt":
            cache.chmod(0o700)
        # The SDK requires one process cache root. Explicit cookies are always
        # supplied; its per-session cache lookup cannot fall back to another SID.
        os.environ.setdefault("GEMINI_COOKIE_PATH", str(cache))

    def items(self):
        return [
            dict(item, key=item["provider"] + "/" + item["id"], icon=PROVIDERS[item["provider"]][1])
            for item in self.api.settings.snapshot()["accounts"]["items"]
        ]

    def active(self):
        return self.api.settings.snapshot()["accounts"]["active"]

    def item(self, key):
        return next(item for item in self.items() if item["key"] == key)

    def add(self, provider, label):
        if (
            provider not in PROVIDERS
            or not isinstance(label, str)
            or not label.strip()
            or len(label) > 80
        ):
            raise ValueError("Sağlayıcı ve 1–80 karakterlik hesap adı gerekir")
        data = self.api.settings.snapshot()["accounts"]
        value = {
            "provider": provider,
            "id": uuid.uuid4().hex,
            "label": label.strip(),
            "port": 9230 + len(data["items"]),
        }
        data["items"].append(value)
        self.api.settings.update("accounts", data)
        return provider + "/" + value["id"]

    def select(self, key):
        self.item(key)
        self.api.settings.update("accounts", {"active": key})
        self.api.settings.update("providers", {"default": key.split("/")[0]})

    def directory(self, key):
        self.item(key)
        return self.root / "data" / "accounts" / key

    def session(self, key):
        item = self.item(key)
        if key == "chatgpt/default":
            return self.api.get("backend").create_session()
        directory = self.directory(key)
        return BrowserSession(
            SessionPaths(
                directory / "session.json", directory / "cache", directory / "chrome-profile"
            ),
            f"http://127.0.0.1:{item['port']}",
        )

    def capabilities(self, key):
        return self.provider(key).capabilities

    def provider(self, key):
        if key not in self._providers:
            item = self.item(key)
            self._providers[key] = (
                ChatGPTProvider(self.session(key), item["id"])
                if item["provider"] == "chatgpt"
                else GeminiProvider(self.directory(key) / "cookies.json", item["id"])
            )
        return self._providers[key]

    def _start(self):
        with self._lock:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(
                    target=self._loop.run_forever, name="siope-providers", daemon=True
                )
                self._thread.start()

    def call(self, coroutine):
        self._start()
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result()

    def close(self):
        if self._loop is not None:

            async def cleanup():
                current = asyncio.current_task()
                pending = [task for task in asyncio.all_tasks() if task is not current]
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for provider in self._providers.values():
                    close = getattr(provider, "close", None)
                    if close:
                        await close()

            self.call(cleanup())
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
            self._loop.close()
            self._loop = None

    def status(self, key):
        path = (
            self.session(key).paths.session_file
            if key.startswith("chatgpt/")
            else self.directory(key) / "cookies.json"
        )
        return {
            "saved_snapshot": path.is_file(),
            "authenticated": key in self._verified,
            "account": self.item(key)["label"],
            "provider": key.split("/")[0],
        }

    def session_action(self, key, action):
        item = self.item(key)
        if action == "status":
            return self.status(key)
        if action == "default_browser":
            if not webbrowser.open(PROVIDERS[item["provider"]][2], new=2):
                raise RuntimeError("Tarayıcı açılamadı")
            return {"opened": True}
        if action == "connect_browser":
            return self.session(key).launch(PROVIDERS[item["provider"]][2])
        provider = self.provider(key)
        if action == "logout":
            self.call(provider.forget_session())
            self._verified.discard(key)
            return dict(self.status(key), logged_out=True)
        if action == "verify":
            if item["provider"] == "gemini":
                session = self.session(key)
                script = Path(__file__).parent / "providers" / "scripts" / "cookies.cjs"
                result = subprocess.run(
                    ["node", str(script)],
                    input=json.dumps(
                        {"cdp_url": session.cdp_url, "origin": PROVIDERS["gemini"][2]}
                    ),
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if result.returncode:
                    raise RuntimeError("Gemini açık bağlı tarayıcı bulunamadı; önce giriş yapın")
                cookies = json.loads(result.stdout)
                if not cookies.get("__Secure-1PSID"):
                    raise RuntimeError("Gemini hesabına giriş tamamlanmamış")
                directory = self.directory(key)
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                self.api.get("backend").atomic_json_write(directory / "cookies.json", cookies)
                self.call(provider.close())
            status = self.call(provider.authenticate())
            if status.state != SessionState.AUTHENTICATED:
                raise RuntimeError("Oturum doğrulanamadı")
            self._verified.add(key)
            return self.status(key)
        raise ValueError("unknown account operation")

    def cached(self, key, remote):
        return self.cache(key).load_cached_conversation(remote)

    def cache(self, key):
        return BackendService(self.directory(key))

    def chat_record(self, key, chat):
        return {
            "id": identity(key, chat.origin.remote_id),
            "remote_id": chat.origin.remote_id,
            "context": key,
            "provider_id": chat.origin.provider_id,
            "account_id": chat.origin.account_id,
            "title": chat.title,
            "project_id": identity(key, chat.project_id),
            "messages": [
                {
                    "id": message.id,
                    "role": message.role,
                    "channel": message.channel,
                    "text": "\n".join(content.text or "" for content in message.content),
                }
                for message in chat.messages
            ],
        }

    def list_chats(self, selected=None, cursor=None):
        cursors = json.loads(cursor) if cursor else None
        accounts = [
            item
            for item in self.items()
            if (selected is None or item["provider"] in selected)
            and (cursors is None or item["key"] in cursors)
            and self.status(item["key"])["saved_snapshot"]
        ]

        async def collect():
            return await asyncio.gather(
                *(
                    self.provider(item["key"]).list_chats(
                        cursor=cursors.get(item["key"]) if cursors else None, limit=50
                    )
                    for item in accounts
                ),
                return_exceptions=True,
            )

        pages = self.call(collect()) if accounts else []
        items, errors, next_cursors = [], [], {}
        for account, page in zip(accounts, pages):
            if isinstance(page, BaseException):
                errors.append(account["label"])
                continue
            items.extend(self.chat_record(account["key"], chat) for chat in page.items)
            if page.next_cursor is not None:
                next_cursors[account["key"]] = page.next_cursor
        return {
            "items": items,
            "cursor": json.dumps(next_cursors) if next_cursors else None,
            "errors": errors,
        }

    def list_projects(self, key, cursor=None):
        provider = self.provider(key)
        if Capability.PROJECTS not in provider.capabilities:
            return {"items": [], "cursor": None}
        page = self.call(provider.list_projects(cursor))
        return {
            "items": [self.project_record(key, project) for project in page.items],
            "cursor": page.next_cursor,
        }

    @staticmethod
    def project_record(key, project):
        return {
            "id": identity(key, project.origin.remote_id),
            "name": project.name,
            "instructions": project.instructions,
            "resources": [asdict(resource) for resource in project.resources],
            "context": key,
        }

    def load(self, value, key):
        key, remote = origin(value, key)
        chat = self.call(self.provider(key).get_chat(remote))
        value = self.chat_record(key, chat)
        self.cache(key).cache_conversation(dict(value, id=remote), make_active=False)
        return value

    def send(
        self,
        key,
        selected_id,
        prompt,
        attachments,
        on_event,
        on_connection,
        model=None,
        effort=None,
        **options,
    ):
        context, remote = origin(selected_id, key)
        project_context, project = origin(options.get("project_id"), key)
        if context != key or project_context != key:
            raise ValueError("Sohbet ve proje seçili hesaba ait olmalı")
        request = SendRequest(
            key.split("/")[0],
            key.split("/")[1],
            selected_id or uuid.uuid4().hex,
            prompt,
            remote,
            model,
            effort,
            project,
            options.get("temporary", False),
            tuple(attachments),
        )
        provider = self.provider(key)
        stream_id = uuid.uuid4().hex

        async def consume():
            remote_id, text, reasoning = remote, "", ""
            async for event in provider.stream(request):
                if isinstance(event, ConversationBound):
                    remote_id = event.conversation_id
                elif isinstance(event, (TextDelta, ReplaceText)):
                    on_event(
                        {
                            "type": "delta" if isinstance(event, TextDelta) else "replace",
                            "text": event.text,
                            "channel": event.channel,
                            "message_id": stream_id + ":" + (event.channel or "final"),
                        }
                    )
                    if event.channel == "analysis":
                        reasoning = (
                            reasoning + event.text if isinstance(event, TextDelta) else event.text
                        )
                    else:
                        text = text + event.text if isinstance(event, TextDelta) else event.text
            if not remote_id:
                raise RuntimeError("Sağlayıcı sohbet kimliği döndürmedi")
            prior = self.cached(key, remote_id) or {}
            messages = prior.get("messages", []) + [{"role": "user", "text": prompt}]
            if reasoning:
                messages.append({"role": "reasoning", "text": reasoning})
            messages.append({"role": "assistant", "text": text})
            title = prior.get("title") or prompt[:60]
            record = {
                "id": remote_id,
                "title": title,
                "messages": messages,
                "project_id": options.get("project_id"),
                "context": key,
                "provider_id": request.provider_id,
                "account_id": request.account_id,
                "model": model,
                "temporary": request.temporary,
            }
            if not request.temporary:
                self.cache(key).cache_conversation(record)
            return {
                "conversation_id": identity(key, remote_id),
                "conversation": {"title": title, "default_model_slug": model, "messages": []},
            }

        self._start()
        future = asyncio.run_coroutine_threadsafe(consume(), self._loop)

        class Cancellation:
            def shutdown(self, *_):
                future.cancel()

            def close(self):
                future.cancel()

        on_connection(Cancellation())
        return future.result()

    def upload_image(self, key, data, name, mime, width, height):
        if not data or len(data) > 20 * 1024 * 1024:
            raise ValueError("Görsel boyutu geçersiz")
        if key.startswith("chatgpt/"):
            with ChatGPTClient(self.session(key)) as client:
                return client.upload_image(name, mime, data, width, height)
        path = self.directory(key) / "attachments" / (uuid.uuid4().hex + ".png")
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
            stream.write(data)
        return {"id": path.stem, "name": name, "path": str(path), "mime_type": mime}

    def attach_file(self, key, path):
        path = Path(path).expanduser().resolve(strict=True)
        if not path.is_file() or not 0 < path.stat().st_size <= 20 * 1024 * 1024:
            raise ValueError("Dosya boş olmamalı ve 20 MiB sınırını aşmamalı")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if key.startswith("gemini/"):
            return {"id": uuid.uuid4().hex, "path": str(path), "name": path.name, "mime_type": mime}
        with ChatGPTClient(self.session(key)) as client:
            return client.upload_file(path.name, mime, path.read_bytes())

    def create_project(self, key, name, visibility="private", memory="default"):
        project = self.call(
            self.provider(key).create_project(name, visibility=visibility, memory=memory)
        )
        return self.project_record(key, project)

    def callbacks(self, key):
        provider = self.provider(key)

        def get_project(project):
            context, remote = origin(project, key)
            if context != key:
                raise ValueError("Proje başka hesaba ait")
            return self.project_record(key, self.call(provider.get_project(remote)))

        def project_chats(project, cursor=None):
            _, remote = origin(project, key)
            page = self.call(provider.list_project_chats(remote, cursor))
            return {
                "items": [self.chat_record(key, item) for item in page.items],
                "cursor": page.next_cursor,
            }

        def move(chat, project):
            chat_context, remote = origin(chat, key)
            project_context, project_remote = origin(project, key)
            if chat_context != key or project_context != key:
                raise ValueError("Sohbet ve proje aynı sağlayıcı hesabında olmalı")
            return self.call(provider.move_chat(remote, project_remote))

        def rename(chat, title):
            _, remote = origin(chat, key)
            return self.call(provider.rename_chat(remote, title))

        def forget(chat):
            _, remote = origin(chat, key)
            if hasattr(provider, "forget_temporary"):
                provider.forget_temporary(remote)
            elif hasattr(provider, "_temporary_conversations"):
                provider._temporary_conversations.pop(remote, None)

        return {
            "upload_image": lambda *args: self.upload_image(key, *args),
            "load_conversation": lambda value: self.load(value, key),
            "send_prompt": lambda *args, **kwargs: self.send(key, *args, **kwargs),
            "load_catalog": lambda: self.call(provider.models()),
            "list_projects_callback": lambda cursor=None: self.list_projects(key, cursor),
            "get_project_callback": get_project,
            "list_project_chats_callback": project_chats,
            "move_conversation": move,
            "rename_conversation": rename,
            "session_action": lambda action: self.session_action(key, action),
            "forget_temporary": forget,
            "attach_file_callback": lambda path: self.attach_file(key, path),
        }

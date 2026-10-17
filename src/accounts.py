"""Account-scoped provider orchestration and provenance for the terminal host."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import subprocess
import threading
import time
import uuid
import webbrowser
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote, unquote

from backend import BackendService
from providers import Capability, ConversationBound, ReplaceText, SendRequest, TextDelta
from providers.chatgpt import BrowserSession, ChatGPTClient, ChatGPTProvider, SessionPaths
from providers.gemini import GeminiProvider
from providers.gemini.notebooks import Notebooks
from providers.models import FeatureState, ModelCatalog, SessionState

MODEL_TIMEOUT = 30
HISTORY_TIMEOUT = 20

PROVIDERS = {
    "chatgpt": ("ChatGPT", "❋", "https://chatgpt.com/"),
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
        self.settings = api.settings
        self.closed = False
        self._chat_projects = {}
        self.root = api.get("paths")
        self._providers = {}
        self.cdp_url = None
        self._verified = set()
        self._catalogs = {}
        self._catalog_tasks = {}
        self._catalog_versions = {}
        self._sending = {}
        self._attachment_paths = {}
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
        config = self.settings.snapshot()
        return [
            dict(
                item,
                key=item["provider"] + "/" + item["id"],
                icon=PROVIDERS[item["provider"]][1],
                enabled=config["providers"].get(item["provider"], {}).get("enabled", False),
            )
            for item in config["accounts"]["items"]
        ]

    def enabled(self, provider):
        return self.settings.snapshot()["providers"].get(provider, {}).get("enabled", False)

    def set_enabled(self, provider, enabled):
        with self._lock:
            if not enabled and any(
                count and key.split("/")[0] == provider for key, count in self._sending.items()
            ):
                raise ValueError("Önce bu sağlayıcıdaki yanıtları tamamlayın veya durdurun")
            self.settings.configure_provider(provider, enabled)

    def preference(self, key):
        return self.settings.snapshot()["accounts"].get("preferences", {}).get(key, {})

    def remember(self, key, model, effort):
        self.item(key)
        self.settings.update("accounts", {"preferences": {key: {"model": model, "effort": effort}}})

    def active_accounts(self):
        if self.closed:
            return []
        data = self.settings.snapshot()["accounts"]
        chosen = dict(data.get("selected", {}))
        legacy = data["active"]
        chosen.setdefault(legacy.split("/")[0], legacy)
        for item in self.items():
            chosen.setdefault(item["provider"], item["key"])
        return [self.item(key) for provider, key in chosen.items() if self.enabled(provider)]

    def archive(self):
        if self.closed:
            return None
        try:
            return self.api.get("archive")
        except LookupError:
            return None

    def read_record(self, key, kind, remote, fetch, force=False):
        archive = self.archive()
        if archive:
            try:
                return archive.read(key, kind, remote or "", fetch, force=force)
            except OSError:
                return fetch()
        return fetch()

    def active(self):
        data = self.settings.snapshot()
        key = data["accounts"]["active"]
        if self.enabled(key.split("/")[0]):
            return key
        return self.active_accounts()[0]["key"]

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
        data = self.settings.snapshot()["accounts"]
        value = {
            "provider": provider,
            "id": uuid.uuid4().hex,
            "label": label.strip(),
            "port": 9230 + len(data["items"]),
        }
        data["items"].append(value)
        self.settings.update("accounts", data)
        return provider + "/" + value["id"]

    def select(self, key):
        self.item(key)
        if not self.enabled(key.split("/")[0]):
            raise ValueError("Sağlayıcı devre dışı; önce Ayarlar → Sağlayıcılar bölümünden açın")
        selected = {item["provider"]: item["key"] for item in self.active_accounts()}
        selected[key.split("/")[0]] = key
        self.settings.update("accounts", {"active": key, "selected": selected})

    def directory(self, key):
        self.item(key)
        return self.root / "data" / "accounts" / key

    def session(self, key):
        item = self.item(key)
        if key == "chatgpt/default":
            return self.api.get("backend").create_session(
                self.cdp_url or f"http://127.0.0.1:{item['port']}"
            )
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
        with self._lock:
            if key not in self._providers:
                item = self.item(key)
                self._providers[key] = (
                    ChatGPTProvider(self.session(key), item["id"])
                    if item["provider"] == "chatgpt"
                    else GeminiProvider(
                        self.directory(key) / "cookies.json",
                        item["id"],
                        notebooks=Notebooks(self.session(key).cdp_url, item["id"]),
                    )
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

    def call(self, coroutine, timeout=45):
        self._start()

        async def bounded():
            return await asyncio.wait_for(coroutine, timeout)

        future = asyncio.run_coroutine_threadsafe(bounded(), self._loop)
        try:
            return future.result(timeout=timeout + 1)
        except (TimeoutError, asyncio.TimeoutError, FutureTimeout):
            future.cancel()
            raise TimeoutError(
                "Sağlayıcı işlemi zaman aşımına uğradı; yeniden deneyebilirsiniz"
            ) from None

    def cached_models(self, key):
        cached = self._catalogs.get(key)
        if cached:
            return cached[1]
        path = self.directory(key) / "models.json"
        try:
            if path.stat().st_size > 256 * 1024:
                return None
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("account_key") != key:
                return None
            catalog = ModelCatalog.from_dict(record["catalog"])
            if catalog.state != FeatureState.AVAILABLE:
                return None
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
        self._catalogs.setdefault(key, (float("-inf"), catalog))
        return self._catalogs[key][1]

    def models(self, key, force=False):
        if not self.enabled(key.split("/")[0]):
            raise ValueError("Sağlayıcı devre dışı")

        async def discover():
            cached = self._catalogs.get(key)
            if cached and not force and time.monotonic() - cached[0] < 300:
                return cached[1]
            task = self._catalog_tasks.get(key)
            if task is None:
                version = self._catalog_versions.get(key, 0)

                async def fetch():
                    value = await asyncio.wait_for(self.provider(key).models(), MODEL_TIMEOUT)
                    if not isinstance(value, ModelCatalog):
                        raise ValueError("Sağlayıcı model kataloğu geçersiz")
                    if value.state != FeatureState.AVAILABLE:
                        raise RuntimeError(value.reason or "Model kataloğu şu anda kullanılamıyor")
                    if version == self._catalog_versions.get(key, 0):
                        self._catalogs[key] = (time.monotonic(), value)
                        if value.state == FeatureState.AVAILABLE:
                            try:
                                self.api.get("backend").atomic_json_write(
                                    self.directory(key) / "models.json",
                                    {"account_key": key, "catalog": asdict(value)},
                                )
                            except OSError:
                                pass  # A cache write failure must not discard the live catalog.
                    return value

                task = asyncio.create_task(fetch())
                self._catalog_tasks[key] = task
                task.add_done_callback(lambda done: self._catalog_tasks.pop(key, None))
            return await asyncio.shield(task)

        return self.call(discover())

    def close(self):
        self.closed = True
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
            self._catalog_versions[key] = self._catalog_versions.get(key, 0) + 1
            self._catalogs.pop(key, None)
            (self.directory(key) / "models.json").unlink(missing_ok=True)

            async def forget():
                task = self._catalog_tasks.get(key)
                if task:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await provider.forget_session()

            self.call(forget())
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

    def cached(self, key, remote=None):
        archive = self.archive()
        if archive and remote is None:
            pointer = archive.get(key, "pointer", "active")
            if pointer is not None:
                remote = pointer.get("remote")
                if remote is None:
                    return None
        if archive and remote:
            value = archive.get(key, "conversation", remote)
            if value is not None:
                return value
        return self.cache(key).load_cached_conversation(remote)

    def set_active(self, key, value):
        _, remote = origin(value, key)
        archive = self.archive()
        if archive:
            try:
                archive.put(key, "pointer", "active", {"remote": remote})
            except OSError:
                pass
        self.cache(key).set_active_conversation(remote)

    def cache(self, key):
        return BackendService(self.directory(key), legacy_fallback=key == "chatgpt/default")

    def chat_record(self, key, chat):
        if chat.project_id:
            self._chat_projects[key, chat.origin.remote_id] = chat.project_id
            if len(self._chat_projects) > 2048:
                self._chat_projects.pop(next(iter(self._chat_projects)))
        project = chat.project_id or self._chat_projects.get((key, chat.origin.remote_id))
        return {
            "id": identity(key, chat.origin.remote_id),
            "remote_id": chat.origin.remote_id,
            "context": key,
            "provider_id": chat.origin.provider_id,
            "account_id": chat.origin.account_id,
            "title": chat.title,
            "project_id": identity(key, project),
            "metadata": chat.metadata,
            "messages": [
                {
                    "id": message.id,
                    "role": message.role,
                    "channel": message.channel,
                    "text": "\n".join(content.text or "" for content in message.content),
                    "content": [asdict(content) for content in message.content],
                    "metadata": message.metadata,
                }
                for message in chat.messages
            ],
        }

    def list_chats(self, selected=None, cursor=None, force=False):
        cursors = json.loads(cursor) if cursor else None
        accounts = [
            item
            for item in self.active_accounts()
            if self.enabled(item["provider"])
            and (selected is None or item["provider"] in selected)
            and (cursors is None or item["key"] in cursors)
            and self.status(item["key"])["saved_snapshot"]
        ]

        def page_for(account):
            key = account["key"]
            page_cursor = cursors.get(key) if cursors else None

            def fetch():
                page = self.call(
                    self.provider(key).list_chats(cursor=page_cursor, limit=50), HISTORY_TIMEOUT
                )
                return {
                    "items": [self.chat_record(key, chat) for chat in page.items],
                    "cursor": page.next_cursor,
                }

            return self.read_record(key, "chats", page_cursor, fetch, force)

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=max(1, len(accounts))) as pool:
            futures = [pool.submit(page_for, account) for account in accounts]
            pages = []
            for future in futures:
                try:
                    pages.append(future.result())
                except Exception as error:
                    pages.append(error)
        items, errors, next_cursors, failed = [], [], {}, []
        for account, page in zip(accounts, pages):
            if isinstance(page, BaseException):
                errors.append(account["label"])
                failed.append(account["key"])
                continue
            items.extend(page["items"])
            if page["cursor"] is not None:
                next_cursors[account["key"]] = page["cursor"]
        return {
            "items": items,
            "cursor": json.dumps(next_cursors) if next_cursors else None,
            "errors": errors,
            "failed_accounts": failed,
        }

    def list_projects(self, key, cursor=None, force=False):
        provider = self.provider(key)
        if Capability.PROJECTS not in provider.capabilities:
            return {"items": [], "cursor": None}

        def fetch():
            page = self.call(provider.list_projects(cursor))
            return {
                "items": [self.project_record(key, p) for p in page.items],
                "cursor": page.next_cursor,
            }

        return self.read_record(key, "containers", cursor, fetch, force)

    def all_projects(self, cursor=None, force=False):
        from concurrent.futures import ThreadPoolExecutor

        cursors = json.loads(cursor) if cursor else None
        accounts = [
            item
            for item in self.active_accounts()
            if (cursors is None or item["key"] in cursors)
            and (
                self.status(item["key"])["saved_snapshot"]
                or (
                    self.archive() and self.archive().get(item["key"], "containers", "") is not None
                )
            )
        ]
        items, errors, failed, more = [], [], [], {}
        with ThreadPoolExecutor(max_workers=max(1, len(accounts))) as pool:
            futures = [
                (
                    account,
                    pool.submit(
                        self.list_projects,
                        account["key"],
                        cursors.get(account["key"]) if cursors else None,
                        force,
                    ),
                )
                for account in accounts
            ]
            for account, future in futures:
                try:
                    page = future.result()
                    items.extend(page["items"])
                    if page.get("cursor"):
                        more[account["key"]] = page["cursor"]
                except Exception:
                    errors.append(account["label"])
                    failed.append(account["key"])
        return {
            "items": items,
            "cursor": json.dumps(more) if more else None,
            "errors": errors,
            "failed_accounts": failed,
        }

    @staticmethod
    def project_record(key, project):
        return {
            "id": identity(key, project.origin.remote_id),
            "name": project.name,
            "remote_id": project.origin.remote_id,
            "instructions": project.instructions,
            "resources": [asdict(resource) for resource in project.resources],
            "context": key,
            "provider_id": key.split("/")[0],
            "kind": project.metadata.get("kind", "project"),
            "metadata": project.metadata,
        }

    def load(self, value, key, force=False):
        key, remote = origin(value, key)
        archive = self.archive()
        if archive and archive.get(key, "conversation", remote) is None:
            legacy = self.cache(key).load_cached_conversation(remote)
            if legacy and not legacy.get("temporary"):
                archive.put(
                    key, "conversation", remote, dict(legacy, id=identity(key, remote), context=key)
                )

        def fetch():
            chat = self.call(self.provider(key).get_chat(remote))
            record = self.chat_record(key, chat)
            if not record.get("project_id"):
                record["project_id"] = (self.cached(key, remote) or {}).get("project_id")
            if not archive:
                self.cache(key).cache_conversation(dict(record, id=remote), make_active=False)
            return record

        return self.read_record(key, "conversation", remote, fetch, force)

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
        if not self.enabled(key.split("/")[0]):
            raise ValueError("Sağlayıcı devre dışı")
        if key not in {item["key"] for item in self.active_accounts()}:
            raise ValueError(
                "Bu sağlayıcıda başka hesap etkin; Hesaplar bölümünden bu hesabı etkinleştirin"
            )
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
            cache_warning = None
            try:
                prior = {} if request.temporary else (self.cached(key, remote_id) or {})
            except OSError:
                prior = {}
                cache_warning = "Yanıt alındı ancak yerel geçmiş okunamadı"

            resources = []
            archive = self.archive()
            if archive and not request.temporary:
                for attachment in attachments:
                    resource = dict(attachment)
                    path = resource.get("path") or self._attachment_paths.get(
                        (key, resource.get("id") or resource.get("file_id"))
                    )
                    if (
                        path
                        and Path(path).is_file()
                        and Path(path).stat().st_size <= 20 * 1024 * 1024
                    ):
                        try:
                            resource["path"], resource["sha256"] = archive.files.save(
                                Path(path).read_bytes()
                            )
                        except OSError:
                            cache_warning = "Yanıt alındı ancak ek dosya arşivlenemedi"

                    resources.append(resource)
            messages = prior.get("messages", []) + [
                {"role": "user", "text": prompt, "resources": resources}
            ]
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
                try:
                    archive = self.archive()
                    if archive:
                        archive.put(
                            key,
                            "conversation",
                            remote_id,
                            dict(record, id=identity(key, remote_id)),
                        )
                        self.set_active(key, remote_id)
                    else:
                        self.cache(key).cache_conversation(record)
                except (OSError, ValueError):
                    cache_warning = "Yanıt alındı ancak yerel arşive kaydedilemedi"
            return {
                "conversation_id": identity(key, remote_id),
                "cache_warning": cache_warning,
                "conversation": {"title": title, "default_model_slug": model, "messages": []},
            }

        self._start()
        future = asyncio.run_coroutine_threadsafe(consume(), self._loop)

        class Cancellation:
            def shutdown(self, *_):
                future.cancel()

            def close(self):
                future.cancel()

        with self._lock:
            self._sending[key] = self._sending.get(key, 0) + 1
        try:
            on_connection(Cancellation())
            return future.result()
        finally:
            with self._lock:
                self._sending[key] -= 1

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
            result = client.upload_file(path.name, mime, path.read_bytes())
        remote = result.get("id") or result.get("file_id")
        if remote:
            self._attachment_paths[key, remote] = str(path)
            if len(self._attachment_paths) > 64:
                self._attachment_paths.pop(next(iter(self._attachment_paths)))
        return result

    def create_project(self, key, name, visibility="private", memory="default"):
        project = self.call(
            self.provider(key).create_project(name, visibility=visibility, memory=memory)
        )
        return self.project_record(key, project)

    def callbacks(self, key):
        provider = self.provider(key)

        def get_project(project):
            context, remote = origin(project, key)
            return self.read_record(
                context,
                "container",
                remote,
                lambda: self.project_record(
                    context, self.call(self.provider(context).get_project(remote))
                ),
            )

        def project_chats(project, cursor=None):
            context, remote = origin(project, key)

            def fetch():
                page = self.call(self.provider(context).list_project_chats(remote, cursor))
                return {
                    "items": [self.chat_record(context, item) for item in page.items],
                    "cursor": page.next_cursor,
                }

            return self.read_record(
                context, "container_chats", remote + ":" + (cursor or ""), fetch
            )

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
            "load_catalog": lambda: self.models(key),
            "list_projects_callback": lambda cursor=None: self.list_projects(key, cursor),
            "get_project_callback": get_project,
            "list_project_chats_callback": project_chats,
            "move_conversation": move,
            "rename_conversation": rename,
            "session_action": lambda action: self.session_action(key, action),
            "forget_temporary": forget,
            "attach_file_callback": lambda path: self.attach_file(key, path),
        }

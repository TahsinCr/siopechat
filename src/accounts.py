"""Account-scoped provider orchestration and provenance for the terminal host."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import CancelledError, Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import asdict, fields, replace
from pathlib import Path
from urllib.parse import quote, unquote

from account_repository import AccountRepository
from backend import BackendService
from providers import (
    BranchContext,
    Capability,
    MessageAction,
    MessageMetadata,
    ReplaceText,
    SendRequest,
    TextDelta,
)
from providers.chatgpt import BrowserSession, ChatGPTClient, ChatGPTProvider, SessionPaths
from providers.claude import ClaudeProvider
from providers.errors import AccountPaused, ProviderError, RemoteNotFound
from providers.gemini import GeminiProvider
from providers.models import FeatureState, ModelCatalog, SessionState
from providers.profiles import import_cookies
from providers.registry import PROVIDERS
from providers.session import read_session, save_session, session_exists
from utils.compatibility import ExceptionGroup

CATALOG_SCHEMA = 2
MODEL_TIMEOUT = 30
HISTORY_TIMEOUT = 20
_LOG = logging.getLogger(__name__)


def identity(context, remote):
    return "@" + context + "/" + quote(remote, safe="") if remote else None


def origin(value, fallback="chatgpt/default"):
    if value and value.startswith("@"):
        provider, account, remote = value[1:].split("/", 2)
        return provider + "/" + account, unquote(remote)
    return fallback, value


def _shallow_dataclass(value):
    """Project provider records without recursively duplicating metadata trees."""
    return {item.name: getattr(value, item.name) for item in fields(value)}


class Accounts:
    def __init__(self, api):
        self.api = api
        self.settings = api.settings
        self.closing = False
        self.closed = False
        self._chat_projects = {}
        self.root = api.get("paths")
        self.repository = AccountRepository(self.root / "data" / "accounts.sqlite3")
        self._providers = {}
        self._credential_stores = {}
        self._credential_lock = threading.Lock()
        self.cdp_url = None
        self._verified = set()
        self._catalogs = {}
        self._catalog_tasks = {}
        self._catalog_versions = {}
        self._catalog_io_lock = threading.Lock()
        self._sending = {}
        self._session_changes = set()
        self._attachment_paths = {}
        self._loop = None
        self._thread = None
        self._lock = threading.Lock()
        self._active_pending = {}
        self._active_values = {}
        self._active_writer_running = False
        self._active_writer_task = None
        self._legacy_cache_migration_lock = threading.Lock()
        self._legacy_cache_migration_complete = False
        for item in self.items():
            key = item["key"]
            for kind, filename in (
                ("catalog", "models.json"),
                ("verification", "verification.json"),
            ):
                path = self.directory(key) / filename
                if not path.is_file():
                    continue
                try:
                    if path.stat().st_size > 256 * 1024:
                        continue
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(value, dict) or value.get("account_key") != key:
                        continue
                    if self.repository.get(key, kind) is None:
                        self.repository.put(key, kind, value)
                    path.unlink(missing_ok=True)
                except (OSError, ValueError):
                    continue

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
        if self.closing or self.closed:
            return []
        data = self.settings.snapshot()["accounts"]
        chosen = dict(data.get("selected", {}))
        legacy = data["active"]
        if legacy:
            chosen.setdefault(legacy.split("/")[0], legacy)
        for item in self.items():
            chosen.setdefault(item["provider"], item["key"])
        return [self.item(key) for provider, key in chosen.items() if self.enabled(provider)]

    def archive(self):
        if self.closed:
            return None
        try:
            archive = self.api.get("archive")
            return None if getattr(archive, "closed", False) is True else archive
        except LookupError:
            return None

    def read_record(self, key, kind, remote, fetch, force=False, *, window=False, fresh=False):
        archive = self.archive()
        if archive:
            try:
                options = {"window": True} if window else {}
                if fresh:
                    options["fresh"] = True
                return archive.read(key, kind, remote or "", fetch, force=force, **options)
            except OSError:
                return fetch()
        return fetch()

    def active(self):
        data = self.settings.snapshot()
        key = data["accounts"]["active"]
        if key and self.enabled(key.split("/")[0]):
            return key
        active = self.active_accounts()
        return active[0]["key"] if active else ""

    def item(self, key):
        item = next((item for item in self.items() if item["key"] == key), None)
        if item is None:
            raise ValueError("Hesap bulunamadı")
        return item

    def add(self, provider, label, browser_account=None):
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
            "port": next(
                port
                for port in range(9230, 65536)
                if port not in {x["port"] for x in data["items"]}
            ),
        }
        if browser_account:
            linked = self.item(browser_account)
            if linked["provider"] == provider or linked.get("browser_account"):
                raise ValueError("Paylaşılan tarayıcı farklı sağlayıcının bağımsız hesabı olmalı")
            value["browser_account"] = browser_account
        data["items"].append(value)
        if not data.get("active"):
            data["active"] = provider + "/" + value["id"]
        self.settings.update("accounts", data)
        return provider + "/" + value["id"]

    def select(self, key):
        self.item(key)
        if not self.enabled(key.split("/")[0]):
            raise ValueError("Sağlayıcı devre dışı; önce Ayarlar → Sağlayıcılar bölümünden açın")
        selected = {item["provider"]: item["key"] for item in self.active_accounts()}
        selected[key.split("/")[0]] = key
        self.settings.update("accounts", {"active": key, "selected": selected})

    def rename(self, key, label):
        """Rename the local display label while keeping the provider session intact."""
        self.settings.rename_account(key, label)
        return self.item(key)

    def remove(self, key):
        """Forget a local account; never delete the remote account or archived conversations."""
        self.item(key)
        with self._lock:
            if self._sending.get(key, 0) or key in self._session_changes:
                raise RuntimeError("Önce devam eden işlemi tamamlayın veya durdurun")
            self._session_changes.add(key)
        try:
            # Reject before closing clients or touching credentials.
            if not any(item["key"] != key and item["enabled"] for item in self.items()):
                raise ValueError("Son etkin hesap silinemez; önce başka bir hesap ekleyin")

            async def release():
                task = self._catalog_tasks.get(key)
                if task:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                provider = self._providers.get(key)
                if provider:
                    await provider.close()

            self.call(release())
            previous_accounts = self.settings.snapshot()["accounts"]
            # Commit visible account removal first. If SQLite cleanup then fails,
            # restore the metadata while keeping the credential record intact.
            active = self.settings.remove_account(key)
            try:
                self.repository.delete(key)
            except Exception as error:
                try:
                    self.settings.update("accounts", previous_accounts)
                except Exception as rollback_error:
                    raise RuntimeError(
                        "Hesap silme tamamlanamadı; ayar geri yüklemesi de başarısız oldu. "
                        f"Özgün hata: {error}; geri yükleme hatası: {rollback_error}"
                    ) from rollback_error
                raise
            with self._catalog_io_lock:
                self._catalog_versions[key] = self._catalog_versions.get(key, 0) + 1
                self._catalogs.pop(key, None)
            self._providers.pop(key, None)
            with self._credential_lock:
                self._credential_stores.pop(key, None)
            self._verified.discard(key)
            with self._lock:
                self._active_pending.pop(key, None)
                self._active_values.pop(key, None)
            self._chat_projects = {k: v for k, v in self._chat_projects.items() if k[0] != key}
            return {"removed": key, "active_account": active}
        finally:
            with self._lock:
                self._session_changes.discard(key)

    def directory(self, key):
        self.item(key)
        return self.root / "data" / "accounts" / key

    def session(self, key):
        item = self.item(key)
        if item.get("browser_account"):
            return self.session(item["browser_account"])
        directory = self.directory(key)
        if key == "chatgpt/default":
            session = self.api.get("backend").create_session(
                self.cdp_url or f"http://127.0.0.1:{item['port']}"
            )
            return BrowserSession(
                replace(session.paths, session_file=self.credentials(key)), session.cdp_url
            )
        return BrowserSession(
            SessionPaths(self.credentials(key), directory / "cache", directory / "chrome-profile"),
            f"http://127.0.0.1:{item['port']}",
        )

    def credentials(self, key):
        with self._credential_lock:
            existing = self._credential_stores.get(key)
            if existing is not None:
                return existing
            store = self.repository.credentials(key)
            if not store.exists():
                directory = self.directory(key)
                if key == "chatgpt/default":
                    old = self.api.get("backend").auth_store_path()
                else:
                    old = directory / (
                        "session.json"
                        if key.startswith("chatgpt/")
                        else "native.json"
                        if key.startswith("claude/")
                        else "cookies.json"
                    )
                if old.is_file():
                    store.save(read_session(old))
                    old.unlink(missing_ok=True)
            self._credential_stores[key] = store
            return store

    def capabilities(self, key):
        return self.provider(key).capabilities

    def provider(self, key):
        with self._lock:
            if key not in self._providers:
                item = self.item(key)
                configuration = {"credential_file": self.credentials(key)}
                if item["provider"] == "chatgpt":
                    configuration["session"] = self.session(key)
                provider = self.api.providers.create_account(
                    item["provider"], item["id"], **configuration
                )
                self._providers[key] = provider
            return self._providers[key]

    def _start(self, allow_closed=False):
        with self._lock:
            if (self.closing or self.closed) and not allow_closed:
                raise RuntimeError("Hesap hizmeti kapalı")
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(
                    target=self._loop.run_forever, name="siope-providers", daemon=True
                )
                try:
                    self._thread.start()
                except Exception:
                    self._loop.close()
                    self._loop = self._thread = None
                    raise

    def call(self, coroutine, timeout=45, *, _closing=False):
        try:
            self._start(allow_closed=_closing)
        except Exception:
            coroutine.close()
            raise

        async def bounded():
            if _closing:
                # Keep the shutdown coroutine in the run_coroutine_threadsafe
                # task. wait_for() wraps it in a child task; Accounts.close()
                # then mistakes this child/parent pair for background work
                # and cancels its own caller while draining the provider loop.
                return await coroutine
            return await asyncio.wait_for(coroutine, timeout)

        future = asyncio.run_coroutine_threadsafe(bounded(), self._loop)
        try:
            return future.result(timeout=timeout + 1)
        except (TimeoutError, asyncio.TimeoutError, FutureTimeout):
            future.cancel()
            raise TimeoutError(
                "Sağlayıcı işlemi zaman aşımına uğradı; yeniden deneyebilirsiniz"
            ) from None

    def _call_cancellable(self, coroutine, timeout, cancel_event):
        if cancel_event.is_set():
            coroutine.close()
            raise CancelledError()

        completion = Future()
        task_ref = []

        async def bounded():
            return await asyncio.wait_for(coroutine, timeout)

        def finish(task):
            if completion.done():
                return
            if task.cancelled():
                completion.cancel()
                return
            error = task.exception()
            if error is not None:
                completion.set_exception(error)
            else:
                completion.set_result(task.result())

        def start():
            task = self._loop.create_task(bounded())
            task_ref.append(task)
            task.add_done_callback(finish)
            if cancel_event.is_set():
                task.cancel()

        try:
            self._loop.call_soon_threadsafe(start)
        except BaseException:
            coroutine.close()
            raise

        deadline = time.monotonic() + timeout + 1
        while True:
            if cancel_event.is_set():
                if not completion.done():
                    self._loop.call_soon_threadsafe(lambda: task_ref and task_ref[0].cancel())
                try:
                    completion.result(timeout=timeout + 1)
                except CancelledError:
                    pass
                except FutureTimeout:
                    raise TimeoutError(
                        "Sağlayıcı dosya yüklemesi iptal edilirken zaman aşımına uğradı"
                    ) from None
                raise CancelledError()
            if completion.done():
                return completion.result()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                cancel_event.set()
                if task_ref:
                    self._loop.call_soon_threadsafe(task_ref[0].cancel)
                raise TimeoutError(
                    "Sağlayıcı işlemi zaman aşımına uğradı; yeniden deneyebilirsiniz"
                )
            cancel_event.wait(min(0.05, remaining))

    def cached_models(self, key):
        cached = self._catalogs.get(key)
        if cached:
            return cached[1]
        try:
            record = self.repository.get(key, "catalog")
            if record is None:
                return None
            if record.get("account_key") != key or record.get("schema") != CATALOG_SCHEMA:
                return None
            catalog = ModelCatalog.from_dict(record["catalog"])
            if catalog.state != FeatureState.AVAILABLE:
                return None
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
        self._catalogs.setdefault(key, (float("-inf"), catalog))
        return self._catalogs[key][1]

    def prepare_enabled(self):
        """Warm saved sessions for enabled accounts without blocking the UI.

        Failures are returned per account so one expired session cannot prevent
        the other providers from becoming ready. Accounts without saved
        credentials are skipped rather than prompting or opening a browser.
        """
        try:
            self._migrate_legacy_account_cache()
        except Exception as error:
            _LOG.warning("Account cache migration could not finish: %s", error)
        accounts = [
            item
            for item in self.items()
            if item["enabled"] and session_exists(self.credentials(item["key"]))
        ]
        if not accounts:
            return {}

        def prepare(item):
            key = item["key"]
            provider = self.provider(key)
            warmup = getattr(provider, "prepare", None)
            if warmup is None:
                return key, None
            try:
                self.call(warmup(), MODEL_TIMEOUT)
            except AccountPaused as error:
                self._forget_verification(key)
                return key, str(error)
            except Exception as error:
                return key, str(error)
            return key, None

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(3, len(accounts))) as pool:
            results = pool.map(prepare, accounts)
            return {key: error for key, error in results if error}

    def _migrate_legacy_account_cache(self):
        """Move per-account JSON cache state to its existing SQLite owner.

        Conversation history belongs to the archive when that extension is
        available. The account database holds the same fallback records only
        while the archive extension is disabled. Legacy files are removed only
        after a durable destination record exists.
        """
        with self._legacy_cache_migration_lock:
            if self._legacy_cache_migration_complete:
                return
            complete = True
            for item in self.items():
                key = item["key"]
                try:
                    self._migrate_account_cache(key)
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                    complete = False
                    _LOG.warning("Could not migrate local cache for %s: %s", key, error)
            self._legacy_cache_migration_complete = complete

    def _migrate_account_cache(self, key):
        backend = self.cache(key)
        archive = self.archive()

        def cached_records(kind):
            after = ""
            while True:
                limit = 1 if kind == "conversation" else 64
                rows = self.repository.cache_records_page(key, kind, after=after, limit=limit)
                if not rows:
                    return
                after = rows[-1][0]
                yield from rows

        def save_conversation(remote, conversation):
            if not isinstance(conversation, dict) or conversation.get("temporary"):
                return False
            saved_id = conversation.get("remote_id") or conversation.get("id") or remote
            context, saved_remote = origin(saved_id, key)
            if context != key or saved_remote != remote:
                return False
            record = dict(conversation)
            record.update(id=identity(key, remote), remote_id=remote, context=key)
            if archive:
                if archive.deleted(key, "conversation", remote):
                    return True
                if archive.conversation_window(key, remote) is None:
                    archive.put(key, "conversation", remote, record)
                return archive.conversation_window(key, remote) is not None
            if self.repository.cache_is_deleted(key, remote):
                return True
            if not self.repository.cache_exists(key, "conversation", remote):
                self.repository.cache_put(key, "conversation", remote, record)
            return self.repository.cache_exists(key, "conversation", remote)

        if archive:
            # Bring forward fallback rows from runs where the archive extension
            # was disabled, without replacing a newer canonical archive record.
            for remote, _ in cached_records("tombstone"):
                if not archive.deleted(key, "conversation", remote):
                    archive.delete(key, "conversation", remote)
                if archive.deleted(key, "conversation", remote):
                    self.repository.cache_delete(key, "tombstone", remote)
            for remote, conversation in cached_records("conversation"):
                if save_conversation(remote, conversation):
                    self.repository.cache_delete(key, "conversation", remote)

        cache_dir = backend.conversation_cache_dir()
        if cache_dir.is_dir():
            for marker in sorted(cache_dir.glob("*.deleted")):
                remote = marker.stem
                try:
                    conversation_path = backend.conversation_cache_path(remote)
                except Exception:
                    continue
                if archive:
                    if not archive.deleted(key, "conversation", remote):
                        archive.delete(key, "conversation", remote)
                    durable = archive.deleted(key, "conversation", remote)
                else:
                    self.repository.cache_mark_deleted(key, remote)
                    durable = self.repository.cache_is_deleted(key, remote)
                if durable:
                    conversation_path.unlink(missing_ok=True)
                    marker.unlink(missing_ok=True)

            for path in sorted(cache_dir.glob("*.json")):
                remote = path.stem
                try:
                    backend.conversation_cache_path(remote)
                    conversation = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if archive and archive.deleted(key, "conversation", remote):
                    durable = True
                else:
                    durable = save_conversation(remote, conversation)
                if durable:
                    path.unlink(missing_ok=True)

        active_path = backend.active_conversation_path()
        legacy_remote = None
        if active_path.is_file():
            try:
                pointer = json.loads(active_path.read_text(encoding="utf-8"))
                candidate = pointer.get("conversation_id") if isinstance(pointer, dict) else None
                if isinstance(candidate, str) and candidate:
                    context, legacy_remote = origin(candidate, key)
                    if context != key:
                        legacy_remote = None
            except (OSError, ValueError, json.JSONDecodeError):
                legacy_remote = None

        if archive:
            current = archive.get(key, "pointer", "active")
            fallback_remote = self.repository.cache_active(key)
            if fallback_remote and archive.deleted(key, "conversation", fallback_remote):
                self.repository.cache_set_active(key, None)
                fallback_remote = None
            if legacy_remote and archive.deleted(key, "conversation", legacy_remote):
                active_path.unlink(missing_ok=True)
                legacy_remote = None
            target = current.get("remote") if isinstance(current, dict) else None
            if target and archive.deleted(key, "conversation", target):
                target = None
                archive.put(key, "pointer", "active", {"remote": None})
                current = archive.get(key, "pointer", "active")
            if current is None:
                target = fallback_remote or legacy_remote
                if target and archive.deleted(key, "conversation", target):
                    target = None
                if target:
                    archive.put(key, "pointer", "active", {"remote": target})
                    current = archive.get(key, "pointer", "active")
            durable = current is not None or not (fallback_remote or legacy_remote)
            if durable:
                self.repository.cache_set_active(key, None)
                if legacy_remote is not None:
                    active_path.unlink(missing_ok=True)
        else:
            current = self.repository.cache_active(key)
            if current is None and legacy_remote:
                self.repository.cache_set_active(key, legacy_remote)
                current = self.repository.cache_active(key)
            if current is not None and legacy_remote is not None:
                active_path.unlink(missing_ok=True)

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
                    try:
                        value = await asyncio.wait_for(
                            self.provider(key).models(force=force), MODEL_TIMEOUT
                        )
                    except AccountPaused:
                        self._forget_verification(key)
                        raise
                    if not isinstance(value, ModelCatalog):
                        raise ValueError("Sağlayıcı model kataloğu geçersiz")
                    if value.state != FeatureState.AVAILABLE:
                        raise RuntimeError(value.reason or "Model kataloğu şu anda kullanılamıyor")
                    if version == self._catalog_versions.get(key, 0):
                        self._catalogs[key] = (time.monotonic(), value)

                        def save_catalog():
                            # Logout and an old discovery result cannot race a
                            # delayed atomic write back onto disk.
                            with self._catalog_io_lock:
                                if version != self._catalog_versions.get(key, 0):
                                    return
                                if (
                                    getattr(
                                        getattr(self.provider(key), "transport", None),
                                        "authenticated",
                                        False,
                                    )
                                    is True
                                ):
                                    self._remember_verification(key)
                                try:
                                    self.repository.put(
                                        key,
                                        "catalog",
                                        {
                                            "schema": CATALOG_SCHEMA,
                                            "account_key": key,
                                            "catalog": asdict(value),
                                        },
                                    )
                                except OSError:
                                    pass

                        saving = asyncio.create_task(
                            asyncio.to_thread(save_catalog), name="siope-persist:" + key
                        )
                        await asyncio.shield(saving)
                    return value

                task = asyncio.create_task(fetch())
                self._catalog_tasks[key] = task

                def forget_task(done):
                    # A cancelled tab may stop awaiting this shared discovery.
                    if not done.cancelled():
                        done.exception()
                    if self._catalog_tasks.get(key) is done:
                        self._catalog_tasks.pop(key, None)

                task.add_done_callback(forget_task)
            return await asyncio.shield(task)

        return self.call(discover())

    def close(self):
        with self._lock:
            if self.closed:
                return
            self.closing = True
            loop, thread = self._loop, self._thread
        if loop is None or thread is None:
            with self._lock:
                self.closed = True
            return

        async def cleanup():
            errors = []
            try:
                await self.api.tasks.close()
            except Exception as error:
                errors.append(error)
            with self._lock:
                pending_active = bool(self._active_pending)
                if pending_active:
                    self._active_writer_running = True
            if pending_active or self._active_writer_running:
                self._ensure_active_writer()
            current = asyncio.current_task()
            pending = [task for task in asyncio.all_tasks() if task is not current]
            persistence = [task for task in pending if task.get_name().startswith("siope-persist:")]
            background = [task for task in pending if task not in persistence]
            for task in background:
                task.cancel()
            # Provider/catalog tasks can finish with ordinary account or network
            # errors while cancellation is in flight. Those outcomes belong to
            # their callers; they are not cleanup failures.
            await asyncio.gather(*background, return_exceptions=True)
            # Atomic writes are deliberately allowed to finish before the loop
            # stops so callers can safely remove their temporary data directory.
            results = await asyncio.gather(*persistence, return_exceptions=True)
            errors.extend(result for result in results if isinstance(result, Exception))
            for provider in self._providers.values():
                close = getattr(provider, "close", None)
                if close:
                    try:
                        await close()
                    except Exception as error:
                        errors.append(error)
            if errors:
                raise ExceptionGroup("account shutdown failed", errors)

        cleanup_error = None
        if loop.is_running():
            try:
                self.call(cleanup(), _closing=True)
            except TimeoutError as error:
                message = (
                    "Provider cleanup timed out; its event loop remains open for a later retry"
                )
                _LOG.error(message)
                raise RuntimeError(message) from error
            except Exception as error:
                cleanup_error = error
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError as error:
                cleanup_error = cleanup_error or error
        if thread.is_alive():
            thread.join(timeout=5)
        if thread.is_alive():
            message = "Provider event loop did not stop; it remains open for a later shutdown retry"
            _LOG.error(message)
            raise RuntimeError(message) from cleanup_error
        with self._lock:
            self.closed = True
        if not loop.is_closed():
            loop.close()
        self._loop = self._thread = None
        if cleanup_error is not None:
            raise cleanup_error

    def browser_connected(self, key):
        # A short local socket probe cannot block UI binding on an HTTP timeout.
        import socket
        from urllib.parse import urlsplit

        endpoint = urlsplit(self.session(key).cdp_url)
        try:
            with socket.create_connection((endpoint.hostname, endpoint.port or 80), timeout=0.03):
                return True
        except OSError:
            return False

    def _remember_verification(self, key, *, resume=False):
        self._verified.add(key)
        if resume and self._loop is not None and self._loop.is_running():

            def resume_job_account():
                try:
                    self.api.tasks.resume_account(*key.split("/", 1))
                except AccountPaused:
                    pass  # Verification must not bypass an active server cooldown.

            self._loop.call_soon_threadsafe(resume_job_account)
        try:
            self.repository.put(
                key, "verification", {"account_key": key, "verified_at": time.time()}
            )
        except OSError:
            # Persistence failure cannot invalidate an authenticated provider reply.
            pass

    def _forget_verification(self, key):
        self._verified.discard(key)
        self.repository.delete(key, "verification")
        transport = getattr(self._providers.get(key), "transport", None)
        if transport is not None:
            transport.authenticated = False

    def status(self, key):
        if not key:
            return {
                "saved_snapshot": False,
                "authenticated": False,
                "account": "",
                "provider": "",
                "transport": "http",
            }
        path = self.credentials(key)
        saved = session_exists(path)
        verified = (
            key in self._verified
            or getattr(getattr(self._providers.get(key), "transport", None), "authenticated", False)
            is True
        )
        if not verified:
            try:
                marker = self.repository.get(key, "verification")
                verified = bool(marker and marker.get("account_key") == key)
            except (OSError, ValueError, AttributeError):
                pass
        return {
            "saved_snapshot": saved,
            "authenticated": saved and verified,
            "transport": "http",
            "account": self.item(key)["label"],
            "provider": key.split("/")[0],
        }

    def _verify_profile(self, key):
        # The worker resolves/decrypts a single default profile off the UI loop.
        with self._lock:
            if self._sending.get(key, 0) or key in self._session_changes:
                raise RuntimeError("Önce devam eden işlemi tamamlayın veya durdurun")
            self._session_changes.add(key)
        try:
            browser, cookies = import_cookies(self.item(key)["provider"])

            async def verify_candidate():
                # Validate independently before replacing any working session.
                with tempfile.TemporaryDirectory(prefix="siope-profile-") as folder:
                    path = Path(folder) / "session.json"
                    kind = self.item(key)["provider"]
                    if kind == "chatgpt":
                        session = BrowserSession(
                            replace(self.session(key).paths, session_file=path)
                        )
                        await asyncio.to_thread(session.import_cookies, cookies)
                        candidate = ChatGPTProvider(session, self.item(key)["id"])
                    else:
                        save_session(path, cookies)
                        factory = GeminiProvider if kind == "gemini" else ClaudeProvider
                        candidate = factory(path, self.item(key)["id"])
                    try:
                        state = await (
                            candidate.authenticate(capture_browser=False)
                            if kind == "chatgpt"
                            else candidate.authenticate()
                        )
                        if state.state != SessionState.AUTHENTICATED:
                            raise RuntimeError("Oturum doğrulanamadı")
                    finally:
                        await candidate.close()
                    value = read_session(path)
                    task = self._catalog_tasks.get(key)
                    if task:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    previous = self._providers.get(key)
                    if previous:
                        await previous.close()
                    # No background catalog may persist the old account snapshot.
                    with self._catalog_io_lock:
                        save_session(self.credentials(key), value)
                        self._providers.pop(key, None)
                        self._catalog_versions[key] = self._catalog_versions.get(key, 0) + 1
                        self._catalogs.pop(key, None)
                        self.repository.delete(key, "catalog")
                    self._remember_verification(key, resume=True)

            self.call(verify_candidate(), timeout=90)
            return dict(self.status(key), imported_browser=browser)
        finally:
            with self._lock:
                self._session_changes.discard(key)

    def session_action(self, key, action):
        item = self.item(key)
        if action == "status":
            return self.status(key)
        if action == "remove":
            return self.remove(key)
        if action == "verify_profile":
            return self._verify_profile(key)
        if action == "default_browser":
            # Browser stderr can contain Chromium diagnostics. Launch the
            # stdlib browser handler in a quiet child so it cannot corrupt the
            # terminal UI's alternate screen.
            options = {"creationflags": 0x208} if os.name == "nt" else {"start_new_session": True}
            result = subprocess.run(
                [sys.executable, "-m", "webbrowser", "-t", PROVIDERS[item["provider"]][2]],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
                **options,
            )
            if result.returncode:
                raise RuntimeError("Tarayıcı açılamadı")
            return {"opened": True}
        if action == "connect_browser":
            session = self.session(key)
            url = PROVIDERS[item["provider"]][2]
            result = session.launch(url)
            if result.get("browser") == "already_running":
                helper = Path(__file__).parent / "providers" / "scripts" / "page.cjs"
                opened = subprocess.run(
                    ["node", str(helper)],
                    input=json.dumps({"cdp_url": session.cdp_url, "origin": url}),
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if opened.returncode:
                    raise RuntimeError("Bağlı tarayıcıda giriş sayfası açılamadı")
                result["opened"] = True
            return result
        provider = self.provider(key)
        if action == "logout":
            with self._catalog_io_lock:
                self._catalog_versions[key] = self._catalog_versions.get(key, 0) + 1
                self._catalogs.pop(key, None)
                self.repository.delete(key, "catalog")

            async def forget():
                task = self._catalog_tasks.get(key)
                if task:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await provider.forget_session()

            self.call(forget())
            self._forget_verification(key)
            return dict(self.status(key), logged_out=True)
        if action == "verify":
            if item["provider"] in ("gemini", "claude"):
                session = self.session(key)
                script = Path(__file__).parent / "providers" / "scripts" / "cookies.cjs"
                result = subprocess.run(
                    ["node", str(script)],
                    input=json.dumps(
                        {"cdp_url": session.cdp_url, "origin": PROVIDERS[item["provider"]][2]}
                    ),
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if result.returncode:
                    raise RuntimeError("Hesabın açık giriş tarayıcısı bulunamadı; önce giriş yapın")
                cookies = json.loads(result.stdout)
                required = "__Secure-1PSID" if item["provider"] == "gemini" else "sessionKey"
                if not cookies.get(required):
                    raise RuntimeError("Hesaba giriş tamamlanmamış")
                directory = self.directory(key)
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                self.call(provider.close())
                save_session(self.credentials(key), cookies)
            try:
                status = self.call(provider.authenticate())
            except AccountPaused:
                self._forget_verification(key)
                raise
            if status.state != SessionState.AUTHENTICATED:
                raise RuntimeError("Oturum doğrulanamadı")
            self._remember_verification(key, resume=True)
            return self.status(key)
        raise ValueError("unknown account operation")

    def cached(self, key, remote=None):
        if not key:
            return None
        archive = self.archive()
        if archive and remote is None:
            pointer = archive.get(key, "pointer", "active")
            if pointer is not None:
                remote = pointer.get("remote")
                if remote is None:
                    return None
        if archive and remote:
            if archive.deleted(key, "conversation", remote):
                return None
            value = archive.get(key, "conversation", remote)
            if value is not None:
                return value
        if remote and self.repository.cache_is_deleted(key, remote):
            return None
        if remote:
            value = self.repository.cache_get(key, "conversation", remote)
            if value is not None:
                return value
        return self.cache(key).load_cached_conversation(remote)

    def active_conversation(self, key):
        archive = self.archive()
        if archive:
            pointer = archive.get(key, "pointer", "active")
            if pointer is not None:
                return pointer.get("remote")
        remote = self.repository.cache_active(key)
        if remote:
            return remote
        return self.cache(key).active_conversation_id()

    def cached_window(self, key, remote=None):
        """UI history reads use bounded SQL windows; other API callers retain full history."""
        archive = self.archive()
        remote = remote or self.active_conversation(key)
        if not remote:
            return None
        if archive:
            value = archive.conversation_window(key, remote)
            if value is not None or archive.deleted(key, "conversation", remote):
                return value
        return self.cached(key, remote)

    def response_version(self, key, conversation_id, message_id, version):
        context, remote = origin(conversation_id, key)
        if context != key or not remote:
            return None
        archive = self.archive()
        return archive.response_version(key, remote, message_id, version) if archive else None

    def _pending_local_branches(self, key, listed, *, project_listing=False):
        remotes = {
            item.get("remote_id") or item.get("id")
            for item in listed
            if isinstance(item, dict)
        }
        remotes = {remote for remote in remotes if remote}
        archive = self.archive()
        if archive:
            archive.confirm_pending_branches(key, remotes, project=project_listing)
            return [
                item
                for item in archive.pending_branches(key, project=project_listing)
                if isinstance(item.get("pending_branch"), dict)
            ]

        pending = []
        for remote, record in self.repository.cache_pending_branches(key):
            if not record.get("pending_local_branch") or not isinstance(
                record.get("pending_branch"), dict
            ):
                continue
            if remote in remotes:
                field = "branch_listed_project" if project_listing else "branch_listed_general"
                record[field] = True
                project_id = record.get("project_id")
                listed_general = record.get("branch_listed_general", False)
                listed_project = record.get("branch_listed_project", False)
                if listed_general and (not project_id or listed_project):
                    record.pop("pending_local_branch", None)
                    record.pop("pending_branch", None)
                    record.pop("branch_unanswered", None)
                    record.pop("branch_listed_general", None)
                    record.pop("branch_listed_project", None)
                self.repository.cache_put(key, "conversation", remote, record)
            else:
                listed_field = (
                    "branch_listed_project" if project_listing else "branch_listed_general"
                )
                if not record.get(listed_field):
                    pending.append(record)
        return pending

    def _include_pending_project_branches(self, key, project_remote, page, cursor=None):
        listed = page.get("items", ())
        pending = self._pending_local_branches(key, listed, project_listing=True)
        if cursor:
            return page
        known = {item.get("remote_id") or item.get("id") for item in listed}
        additions = []
        for item in pending:
            project = item.get("project_id")
            remote = item.get("remote_id") or item.get("id")
            if not project or remote in known:
                continue
            context, project_id = origin(project, key)
            if context == key and project_id == project_remote:
                additions.append(item)
        return dict(page, items=[*listed, *additions]) if additions else page

    def cached_project_chats(self, value, fallback, cursor=None):
        """Return one cached provider project-chat page without starting a refresh."""
        key, remote = origin(value, fallback)
        archive = self.archive()
        if not remote or archive is None or archive.deleted(key, "container", remote):
            return None
        page_key = remote + ":" + (cursor or "")
        page = archive.get(key, "container_chats", page_key)
        if not isinstance(page, dict):
            return None
        return self._include_pending_project_branches(key, remote, page, cursor)

    def queue_active(self, key, value):
        """Coalesce UI pointer writes; never perform SQLite/fsync in navigation."""
        with self._lock:
            if self.closing or self.closed:
                return
            if key in self._active_values and self._active_values[key] == value:
                return
            self._active_values[key] = value
            self._active_pending[key] = value
            if self._active_writer_running:
                return
            self._active_writer_running = True

        try:
            self._start()
            self._loop.call_soon_threadsafe(self._ensure_active_writer)
        except RuntimeError:
            with self._lock:
                closing = self.closing or self.closed
                self._active_writer_running = False
            if closing:
                return
            raise

    def _ensure_active_writer(self):
        task = self._active_writer_task
        if not self._active_writer_running or (task is not None and not task.done()):
            return
        self._active_writer_task = asyncio.create_task(
            self._write_active_queue(), name="siope-persist:active"
        )

    async def _write_active_queue(self):
        while True:
            with self._lock:
                if not self._active_pending:
                    self._active_writer_running = False
                    self._active_writer_task = None
                    return
                account = next(iter(self._active_pending))
                target = self._active_pending.pop(account)
            try:
                await asyncio.to_thread(self.set_active, account, target)
                with self._lock:
                    if (
                        account not in self._active_pending
                        and self._active_values.get(account) == target
                    ):
                        self._active_values.pop(account, None)
            except (OSError, ValueError):
                with self._lock:
                    self._active_values.pop(account, None)

    def set_active(self, key, value):
        _, remote = origin(value, key)
        if remote and self.repository.cache_is_deleted(key, remote):
            remote = None
        archive = self.archive()
        if archive:
            try:
                archive.put(key, "pointer", "active", {"remote": remote})
            except OSError:
                self.repository.cache_set_active(key, remote)
                return
            self.repository.cache_set_active(key, None)
        else:
            self.repository.cache_set_active(key, remote)

    def cache(self, key):
        return BackendService(self.directory(key), legacy_fallback=key == "chatgpt/default")

    def cache_conversation(self, key, conversation, *, make_active=True):
        remote = conversation.get("remote_id") or conversation.get("id")
        context, remote = origin(remote, key)
        if context != key or not remote or conversation.get("temporary"):
            return
        if self.repository.cache_is_deleted(key, remote):
            return
        record = dict(conversation, id=identity(key, remote), remote_id=remote, context=key)
        archive = self.archive()
        if archive:
            if archive.deleted(key, "conversation", remote):
                return
            archive.put(key, "conversation", remote, record)
            self.repository.cache_delete(key, "conversation", remote)
        else:
            if not self.repository.cache_put(key, "conversation", remote, record):
                return
        if make_active:
            self.set_active(key, remote)

    def chat_record(self, key, chat):
        if chat.metadata.get("project_membership_known") and not chat.project_id:
            self._chat_projects.pop((key, chat.origin.remote_id), None)
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
            **(
                {"pending_branch": chat.metadata["pending_branch"]}
                if chat.metadata.get("pending_branch")
                else {}
            ),
            "history_complete": not chat.metadata.get("history_partial", False),
            "messages": [
                {
                    "id": message.id,
                    "role": message.role,
                    "channel": message.channel,
                    "text": "\n".join(content.text or "" for content in message.content),
                    "content": [
                        dict(
                            _shallow_dataclass(content),
                            resource=(
                                _shallow_dataclass(content.resource)
                                if content.resource is not None
                                else None
                            ),
                        )
                        for content in message.content
                    ],
                    "metadata": message.metadata,
                    "actions": list(message.actions),
                }
                for message in chat.messages
            ],
        }

    def list_chats(self, selected=None, cursor=None, force=False, account_keys=None):
        cursors = json.loads(cursor) if cursor else None
        accounts = [
            item
            for item in self.items()
            if (account_keys is None or item["key"] in account_keys)
            and self.enabled(item["provider"])
            and (selected is None or item["provider"] in selected)
            and (cursors is None or item["key"] in cursors)
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
                    "complete": not page_cursor
                    and page.next_cursor is None
                    and len(page.items) < 50,
                }

            page = self.read_record(key, "chats", page_cursor, fetch, force, fresh=force)
            # Closed or tombstoned list-page cache entries must not hide the
            # provider's live chat list. Conversation tombstones remain enforced
            # when the fetched page is normalized by the archive.
            return page if page is not None else fetch()

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(6, max(1, len(accounts)))) as pool:
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
                items.extend(self._pending_local_branches(account["key"], ()))
                continue
            items.extend(page["items"])
            pending = self._pending_local_branches(account["key"], page["items"])
            if not cursors or not cursors.get(account["key"]):
                known = {
                    item.get("remote_id") or item.get("id")
                    for item in page["items"]
                    if isinstance(item, dict)
                }
                items.extend(
                    item
                    for item in pending
                    if (item.get("remote_id") or item.get("id")) not in known
                )
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

        return self.read_record(key, "containers", cursor, fetch, force, fresh=force)

    def all_projects(self, cursor=None, force=False, account_keys=None, on_account=None):
        from concurrent.futures import ThreadPoolExecutor, as_completed

        cursors = json.loads(cursor) if cursor else None
        accounts = [
            item
            for item in self.items()
            if self.enabled(item["provider"])
            and (account_keys is None or item["key"] in account_keys)
            and (cursors is None or item["key"] in cursors)
        ]
        items, errors, failed, more = [], [], [], {}

        def report(account, outcome):
            if on_account is None:
                return
            try:
                on_account(account, outcome)
            except Exception:
                # Progress reporting is an optional UI hint; it cannot turn a
                # valid provider response into a failed account refresh.
                return

        with ThreadPoolExecutor(
            max_workers=min(6, max(1, len(accounts))), thread_name_prefix="project-list"
        ) as pool:
            futures = {
                pool.submit(
                    self.list_projects,
                    account["key"],
                    cursors.get(account["key"]) if cursors else None,
                    force,
                ): account
                for account in accounts
            }
            for future in as_completed(futures):
                account = futures[future]
                try:
                    page = future.result()
                    report(account, page)
                    items.extend(page["items"])
                    if page.get("cursor"):
                        more[account["key"]] = page["cursor"]
                except Exception as error:
                    report(account, error)
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

    def load(self, value, key, force=False, *, window=False):
        key, remote = origin(value, key)
        archive = self.archive()
        if archive and archive.deleted(key, "conversation", remote):
            raise RemoteNotFound("Bu sohbet sağlayıcıdan silindi")
        if archive and archive.conversation_window(key, remote) is None:
            legacy = self.cached(key, remote)
            if legacy and not legacy.get("temporary"):
                archive.put(
                    key, "conversation", remote, dict(legacy, id=identity(key, remote), context=key)
                )

        cached = self.cached_window(key, remote) if archive else self.cached(key, remote)
        pending_branch = bool(
            cached
            and isinstance(cached.get("pending_branch"), dict)
            and cached.get("branch_unanswered", True)
            and (
                archive.is_pending_branch(key, remote)
                if archive and remote
                else cached.get("pending_local_branch")
            )
        )
        if cached and pending_branch:
            return cached

        def fetch():
            try:
                chat = self.call(self.provider(key).get_chat(remote))
            except RemoteNotFound:
                if archive:
                    archive.delete(key, "conversation", remote)
                raise
            record = self.chat_record(key, chat)
            previous = self.cached_window(key, remote) or {}
            if chat.metadata.get("title_unavailable"):
                record["title"] = previous.get("title") or record["title"]
            if not record.get("project_id"):
                record["project_id"] = previous.get("project_id")
            if not archive:
                self.cache_conversation(key, dict(record, id=remote), make_active=False)
            return record

        return self.read_record(key, "conversation", remote, fetch, force, window=window)

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
        if options.get("action"):
            action = MessageAction(options["action"])
            if action == MessageAction.BRANCH or not remote or not options.get("message_id"):
                raise ValueError("Invalid message generation action")
            if attachments or options.get("temporary"):
                raise ValueError("Message actions preserve the original conversation attachments")
        self._start()
        with self._lock:
            if key in self._session_changes:
                raise RuntimeError("Profil doğrulaması tamamlanana kadar bekleyin")
            self._sending[key] = self._sending.get(key, 0) + 1
        cancel_event = threading.Event()
        task = None
        job_id = None
        cancel_requested = False
        self_loop = self._loop

        def cancel():
            nonlocal cancel_requested
            cancel_event.set()
            if not cancel_requested:
                cancel_requested = True
                if task is not None and not task.done():
                    task.cancel()

        class Cancellation:
            @property
            def id(self):
                return job_id

            def shutdown(self, *_):
                self.close()

            def close(self):
                cancel_event.set()
                try:
                    self_loop.call_soon_threadsafe(cancel)
                except RuntimeError:
                    pass

        connection = Cancellation()
        try:
            on_connection(connection)
            if cancel_event.is_set():
                raise CancelledError()
            attachments, omitted_attachments = self._prepare_attachments(
                key, attachments, conversation_id=remote, cancel_event=cancel_event
            )
            if cancel_event.is_set():
                raise CancelledError()
        except BaseException:
            connection.close()
            raise
        finally:
            with self._lock:
                self._sending[key] -= 1
        provider_attachments = tuple(
            {
                name: value
                for name, value in attachment.items()
                if name
                not in {
                    "path",
                    "queue_id",
                    "source_account_key",
                    "source_tab_id",
                    "temporary_local",
                }
            }
            for attachment in attachments
        )
        archive = self.archive()
        if remote and archive and archive.deleted(key, "conversation", remote):
            raise RemoteNotFound("Silinmiş bir sohbete mesaj gönderilemez")
        baseline = archive.conversation_window(key, remote, limit=1) if archive and remote else None
        if not archive and remote:
            baseline = self.cached(key, remote)
        base_end = baseline.get("history_window", {}).get("total") if baseline else None
        pending_branch = (baseline or {}).get("pending_branch")
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
            provider_attachments,
            action=MessageAction(options["action"]) if options.get("action") else None,
            message_id=options.get("message_id"),
            branch=BranchContext(**pending_branch) if pending_branch else None,
        )
        self.provider(key)
        stream_id = uuid.uuid4().hex

        completion = Future()

        async def consume():
            nonlocal job_id

            def guard():
                current_archive = self.archive()
                if current_archive and (
                    (remote and current_archive.deleted(key, "conversation", remote))
                    or (project and current_archive.deleted(key, "container", project))
                ):
                    raise RemoteNotFound("Sohbet veya proje bu sırada silindi")

            def deliver(event):
                if isinstance(event, MessageMetadata):
                    on_event(dict(asdict(event), type="metadata"))
                    return
                if isinstance(event, (TextDelta, ReplaceText)):
                    on_event(
                        {
                            "type": "delta" if isinstance(event, TextDelta) else "replace",
                            "text": event.text,
                            "channel": event.channel,
                            "message_id": stream_id + ":" + (event.channel or "final"),
                        }
                    )

            def persist(snapshot):
                remote_id = snapshot.conversation_id or remote
                if not remote_id:
                    raise RuntimeError("Sağlayıcı sohbet kimliği döndürmedi")
                cache_warning = None
                try:
                    prior = (
                        {}
                        if request.temporary
                        else (
                            self.cached_window(key, remote_id)
                            if self.archive()
                            else self.cached(key, remote_id)
                        )
                        or {}
                    )
                except OSError:
                    prior = {}
                    cache_warning = "Yanıt alındı ancak yerel geçmiş okunamadı"

                resources = []
                archive = self.archive()
                if archive and not request.temporary:
                    for attachment in attachments:
                        resource = dict(attachment)
                        for internal in (
                            "queue_id",
                            "source_tab_id",
                            "source_account_key",
                            "temporary_local",
                        ):
                            resource.pop(internal, None)
                        # Keep the provider's attachment identity and display
                        # metadata, never the user's local path or file bytes.
                        resource.pop("path", None)
                        resource.pop("sha256", None)
                        resources.append(resource)
                messages = ([] if self.archive() else prior.get("messages", [])) + [
                    {
                        "id": (snapshot.metadata.user_message_id if snapshot.metadata else None)
                        or stream_id + ":user",
                        "local_pending": True,
                        "role": "user",
                        "text": prompt,
                        "resources": resources,
                        "actions": ["edit"]
                        if snapshot.metadata
                        and snapshot.metadata.user_message_id
                        and not request.temporary
                        else [],
                    }
                ]
                reasoning_text = snapshot.reasoning
                if reasoning_text:
                    messages.append(
                        {
                            "id": stream_id + ":analysis",
                            "local_pending": True,
                            "role": "reasoning",
                            "text": reasoning_text,
                        }
                    )
                messages.append(
                    {
                        "id": (snapshot.metadata.message_id if snapshot.metadata else None)
                        or stream_id + ":final",
                        "local_pending": True,
                        "role": "assistant",
                        "text": snapshot.text,
                        "actions": list(snapshot.metadata.actions) if snapshot.metadata else [],
                        "metadata": {
                            "model": model,
                            "effort": effort,
                            "sources": list(snapshot.metadata.sources) if snapshot.metadata else [],
                        },
                    }
                )
                title = prior.get("title") or prompt[:60]
                record = {
                    "id": remote_id,
                    "title": title,
                    "messages": messages,
                    "project_id": options["project_id"]
                    if "project_id" in options
                    else prior.get("project_id"),
                    "context": key,
                    "provider_id": request.provider_id,
                    "account_id": request.account_id,
                    "model": model,
                    "temporary": request.temporary,
                }
                if not archive:
                    for field in (
                        "pending_local_branch",
                        "branch_listed_general",
                        "branch_listed_project",
                    ):
                        if prior.get(field):
                            record[field] = prior[field]
                    if prior.get("pending_local_branch"):
                        record["branch_unanswered"] = False
                if not request.temporary:
                    try:
                        archive = self.archive()
                        if archive:
                            archive.save_turn(
                                key,
                                remote_id,
                                dict(
                                    record, id=identity(key, remote_id), _archive_base_end=base_end
                                ),
                            )
                            archive.mark_branch_answered(key, remote_id)
                            self.set_active(key, remote_id)
                        else:
                            self.cache_conversation(key, record)
                    except (OSError, ValueError):
                        cache_warning = "Yanıt alındı ancak yerel arşive kaydedilemedi"
                return {
                    "conversation_id": identity(key, remote_id),
                    "cache_warning": cache_warning,
                    "sent_attachment_ids": [
                        item["queue_id"] for item in attachments if item.get("queue_id")
                    ],
                    "attachment_omitted_count": len(omitted_attachments),
                    "conversation": {"title": title, "default_model_slug": model, "messages": []},
                }

            async def complete(snapshot):
                if not (snapshot.conversation_id or remote):
                    raise ProviderError("Sağlayıcı sohbet kimliği döndürmedi")
                try:
                    if request.action or request.branch:
                        remote_id = snapshot.conversation_id or remote
                        authoritative = self.chat_record(
                            key, await self.provider(key).get_chat(remote_id)
                        )

                        def save_revision():
                            archive = self.archive()
                            if archive:
                                first = next(iter(authoritative.get("messages", ())), {})
                                replacement = (
                                    request.message_id
                                    if (
                                        request.action == MessageAction.EDIT
                                        and snapshot.metadata
                                        and first.get("id") == snapshot.metadata.user_message_id
                                    )
                                    else None
                                )
                                archive.replace_history(
                                    key,
                                    remote_id,
                                    authoritative,
                                    replaced_message_id=replacement,
                                    retry_message_id=(
                                        request.message_id
                                        if request.action == MessageAction.RETRY
                                        else None
                                    ),
                                    new_message_id=(
                                        snapshot.metadata.message_id
                                        if request.action == MessageAction.RETRY
                                        and snapshot.metadata
                                        else None
                                    ),
                                )
                                if request.branch and remote_id != remote:
                                    archive.delete(key, "conversation", remote)
                                    archive.mark_pending_branch(key, remote_id)
                                    archive.mark_branch_answered(key, remote_id)
                                self.set_active(key, remote_id)
                            else:
                                if request.branch:
                                    authoritative["pending_local_branch"] = True
                                    authoritative["branch_unanswered"] = False
                                self.cache_conversation(key, authoritative)
                                if request.branch and remote_id != remote:
                                    self.repository.cache_mark_deleted(key, remote)
                            return {
                                "conversation_id": identity(key, remote_id),
                                "replaced_conversation_id": identity(key, remote)
                                if request.branch and remote != remote_id
                                else None,
                                "conversation": {"title": authoritative["title"], "messages": []},
                                "history": self.cached_window(key, remote_id)
                                if archive
                                else authoritative,
                            }

                        return await asyncio.to_thread(save_revision)
                    return await asyncio.to_thread(persist, snapshot)
                except Exception:
                    # The remote turn has already completed. Local failures must
                    # not invite a duplicate POST or report the accepted send as failed.
                    return {
                        "conversation_id": identity(key, snapshot.conversation_id or remote),
                        "cache_warning": "Yanıt alındı ancak yerel arşive kaydedilemedi",
                        "sent_attachment_ids": [
                            item["queue_id"] for item in attachments if item.get("queue_id")
                        ],
                        "attachment_omitted_count": len(omitted_attachments),
                        "conversation": {
                            "title": prompt[:60],
                            "default_model_slug": model,
                            "messages": [],
                        },
                    }

            jobs = self.api.tasks
            job_id = jobs.submit(
                request, on_event=deliver, on_complete=complete, guard=guard, queue_chat=True
            )
            try:
                return await jobs.result(job_id)
            except asyncio.CancelledError:
                await jobs.cancel(job_id)
                raise
            finally:
                jobs.forget(job_id)

        with self._lock:
            if key in self._session_changes:
                raise RuntimeError("Profil doğrulaması tamamlanana kadar bekleyin")
            if not self.enabled(key.split("/")[0]):
                raise ValueError("Sağlayıcı devre dışı")
            if key not in {item["key"] for item in self.active_accounts()}:
                raise ValueError(
                    "Bu sağlayıcıda başka hesap etkin; Hesaplar bölümünden bu hesabı etkinleştirin"
                )
            self._sending[key] = self._sending.get(key, 0) + 1

            def finished(inner):
                try:
                    completion.set_result(inner.result())
                except asyncio.CancelledError:
                    completion.set_exception(CancelledError())
                except BaseException as error:
                    completion.set_exception(error)

            def start():
                nonlocal task
                task = self._loop.create_task(consume(), name="siope-send:" + key)
                task.add_done_callback(finished)
                if cancel_requested or cancel_event.is_set():
                    task.cancel()

            try:
                self._loop.call_soon_threadsafe(start)
            except BaseException:
                self._sending[key] -= 1
                raise

        try:
            return completion.result()
        except AccountPaused:
            self._forget_verification(key)
            raise
        finally:
            with self._lock:
                self._sending[key] -= 1

    def _prepare_attachments(self, key, attachments, *, conversation_id=None, cancel_event=None):
        """Fit staged files to the destination account and re-upload foreign IDs."""
        if not attachments:
            return [], []
        import mimetypes

        from providers.attachments import fit_attachments

        policy = self.attachment_policy(key)
        if policy is None:
            raise ValueError("Bu sağlayıcı dosya eklerini desteklemiyor")
        if hasattr(policy, "as_dict"):
            policy = policy.as_dict()
        else:
            policy = dict(policy)

        if policy.get("max_files_scope") == "conversation" and conversation_id:
            provider = self.provider(key)
            count_attachments = getattr(provider, "conversation_attachment_count", None)
            if not callable(count_attachments):
                raise ValueError("Bu sohbetin dosya kotası doğrulanamıyor; ekler gönderilmedi")
            coroutine = count_attachments(conversation_id)
            try:
                used_files = (
                    self._call_cancellable(coroutine, HISTORY_TIMEOUT, cancel_event)
                    if cancel_event is not None
                    else self.call(coroutine, timeout=HISTORY_TIMEOUT)
                )
            except BaseException:
                if getattr(coroutine, "close", None):
                    coroutine.close()
                raise
            if isinstance(used_files, bool) or not isinstance(used_files, int) or used_files < 0:
                raise ValueError(
                    "Claude sohbetindeki mevcut dosya sayısı doğrulanamadı; ekler gönderilmedi"
                )
            policy["max_files"] = max(0, int(policy.get("max_files") or 0) - used_files)
            if policy["max_files"] == 0:
                raise ValueError("Bu sohbetin dosya ekleme kotası dolu; yeni ekler gönderilmedi")

        normalized = []
        for attachment in attachments:
            resource = dict(attachment)
            remote = resource.get("id") or resource.get("file_id")
            path = resource.get("path") or self._attachment_paths.get((key, remote))
            if path:
                try:
                    local = Path(path)
                    size = local.stat().st_size
                except (OSError, TypeError, ValueError):
                    pass
                else:
                    resource.setdefault("path", str(local))
                    resource.setdefault("name", local.name)
                    resource.setdefault("mime_type", mimetypes.guess_type(local.name)[0] or "")
                    if resource.get("size_bytes") is None:
                        resource["size_bytes"] = size
            normalized.append(resource)
        selected, omitted = fit_attachments(normalized, policy)
        if not selected:
            raise ValueError("Ekler bu sağlayıcının tür veya boyut sınırlarına uymuyor")

        prepared = []
        for attachment in selected:
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError()
            resource = dict(attachment)
            source_key = resource.get("source_account_key") or key
            if source_key != key:
                path = resource.get("path")
                if not path or not Path(path).is_file():
                    raise ValueError(
                        f"{resource.get('name') or 'Dosya'} hedef hesap için yeniden yüklenemiyor"
                    )
                uploaded = dict(
                    self.attach_file(key, path, cancel_event=cancel_event)
                    if cancel_event is not None
                    else self.attach_file(key, path)
                )
                for name in (
                    "queue_id",
                    "source_tab_id",
                    "path",
                    "name",
                    "size_bytes",
                    "mime_type",
                ):
                    if name in resource:
                        uploaded[name] = resource[name]
                uploaded["source_account_key"] = key
                resource = uploaded
            prepared.append(resource)
        return prepared, omitted

    def resolve_download(self, key, url, conversation_id=None, message_id=None):
        from urllib.parse import urlsplit

        from providers.files import DownloadSource

        context, remote = origin(conversation_id, key)
        if context != key:
            raise ValueError("Dosya seçili hesaba ait olmalı")
        provider = self.provider(key) if key else None
        if provider and hasattr(provider, "resolve_download"):
            source = self.call(provider.resolve_download(url, remote, message_id))
            if source is not None:
                return source
        if urlsplit(url).scheme not in ("http", "https"):
            raise ValueError("Bu dosya sağlayıcı tarafından çözümlenemedi")
        return DownloadSource(url)

    def download(self, key, url, conversation_id=None, message_id=None, progress=None, *, title=""):
        return self.api.get("downloads").fetch(
            key, url, conversation_id, message_id, progress, title=title
        )

    def upload_image(self, key, data, name, mime, width, height):
        if key.startswith("claude/") and Capability.ATTACHMENTS not in self.capabilities(key):
            raise ValueError("Claude dosya ekleme henüz kullanılamıyor")
        limit = 30 if key.startswith("claude/") else 20
        if not data or len(data) > limit * 1024 * 1024:
            raise ValueError("Görsel boyutu geçersiz")
        if key.startswith("chatgpt/"):
            path = self.directory(key) / "attachments" / (uuid.uuid4().hex + ".png")
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            path.write_bytes(data)
            try:
                with ChatGPTClient(self.session(key)) as client:
                    result = client.upload_image(name, mime, data, width, height)
                result.update(
                    path=str(path),
                    size_bytes=len(data),
                    temporary_local=True,
                )
                remote = result.get("id") or result.get("file_id")
                if remote:
                    self._attachment_paths[key, remote] = str(path)
                return result
            except BaseException:
                path.unlink(missing_ok=True)
                raise
        path = self.directory(key) / "attachments" / (uuid.uuid4().hex + ".png")
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
            stream.write(data)
        if key.startswith("claude/"):
            try:
                result = self.attach_file(key, path)
                result["name"] = name
                result["mime_type"] = mime
                result["size_bytes"] = len(data)
                result["temporary_local"] = True
                return result
            except BaseException:
                path.unlink(missing_ok=True)
                raise
        return {
            "id": path.stem,
            "name": name,
            "path": str(path),
            "mime_type": mime,
            "size_bytes": len(data),
            "temporary_local": True,
        }

    def attach_file(self, key, path, *, cancel_event=None):
        path = Path(path).expanduser().resolve(strict=True)
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError("Dosya boş olmamalı")
        provider = self.provider(key)
        if Capability.ATTACHMENTS not in provider.capabilities:
            raise ValueError("Bu sağlayıcıda dosya ekleme kullanılamıyor")
        upload = getattr(provider, "upload_file", None)
        if upload is None:
            raise ValueError("Sağlayıcının dosya yükleme işlemi hazır değil")
        if cancel_event is None:
            result = self.call(upload(path))
        elif key.startswith("chatgpt/"):
            result = self._call_cancellable(
                upload(path, cancel_event=cancel_event), 45, cancel_event
            )
        else:
            result = self._call_cancellable(upload(path), 45, cancel_event)
        if key.startswith("gemini/"):
            return result
        remote = result.get("id") or result.get("file_id")
        if remote:
            self._attachment_paths[key, remote] = str(path)
            if len(self._attachment_paths) > 64:
                self._attachment_paths.pop(next(iter(self._attachment_paths)))
        return result

    def release_attachment(self, attachment):
        """Remove an app-owned temporary clipboard image after its queue ends."""
        if not isinstance(attachment, dict) or not attachment.get("temporary_local"):
            return
        key = attachment.get("source_account_key")
        path_value = attachment.get("path")
        if not key or not path_value:
            return
        try:
            path = Path(path_value).resolve()
            directory = (self.directory(key) / "attachments").resolve()
        except (OSError, RuntimeError, KeyError, ValueError):
            return
        if directory not in path.parents:
            return
        path.unlink(missing_ok=True)
        remote = attachment.get("id") or attachment.get("file_id")
        if remote:
            self._attachment_paths.pop((key, remote), None)

    def attachment_policy(self, key):
        provider = self.provider(key)
        if Capability.ATTACHMENTS not in provider.capabilities:
            return None
        describe = getattr(provider, "attachment_policy", None)
        return describe() if describe else None

    def synchronize(self, scope="all", key=None, conversation_id=None):
        """One explicit revalidation entry point for every frontend."""
        if scope not in ("all", "library", "conversation"):
            raise ValueError("Unknown synchronization scope")
        targets = [self.item(key)] if key else self.active_accounts()
        errors = []
        for item in targets:
            account = item["key"]
            if not self.status(account)["saved_snapshot"]:
                continue
            try:
                if scope == "all":
                    self.models(account, force=True)
                if scope in ("all", "library"):
                    self.list_chats(selected=[item["provider"]], force=True, account_keys={account})
                    self.list_projects(account, force=True)
                    archive = self.archive()
                    if archive:
                        archive.refresh(
                            account=account,
                            kinds={"chats", "containers", "container", "container_chats"},
                            exclude={
                                (account, "chats", ""),
                                (account, "containers", ""),
                            },
                        )
                if scope in ("all", "conversation") and conversation_id:
                    context, _ = origin(conversation_id, account)
                    if context == account:
                        self.load(conversation_id, account, force=True)
            except Exception as error:
                errors.append(item["label"] + ": " + str(error))
        if errors:
            raise RuntimeError(" · ".join(errors))
        return {"scope": scope, "queued": True}

    def create_project(self, key, name, visibility="private", memory="default"):
        project = self.call(
            self.provider(key).create_project(name, visibility=visibility, memory=memory)
        )
        return self.store_project(key, project)

    def project_options(self, key, value=None):
        context, remote = origin(value, key)
        if context != key:
            raise ValueError("Proje seçili hesaba ait olmalı")
        return self.call(self.provider(key).project_options(remote), timeout=90)

    def project_details(self, key, value):
        context, remote = origin(value, key)
        if context != key or not remote:
            raise ValueError("Proje seçili hesaba ait olmalı")
        if not self.status(key)["saved_snapshot"]:
            archive = self.archive()
            cached = archive.get(key, "container", remote) if archive else None
            if cached is None and archive:
                cached = next(
                    (
                        item
                        for item in archive.containers(key, limit=500)
                        if item.get("remote_id") == remote
                        or origin(item.get("id"), key)[1] == remote
                    ),
                    None,
                )
            if cached is None:
                raise RuntimeError("Kayıtlı oturum yok; Hesaplar bölümünden doğrulayın")
            return dict(cached, requires_auth=True)
        return self.store_project(key, self.call(self.provider(key).get_project(remote)))

    def store_project(self, key, project):
        record = self.project_record(key, project)
        archive = self.archive()
        if archive:
            try:
                archive.put(key, "container", project.origin.remote_id, record)
                for state in archive.states():
                    if state["account"] != key or state["kind"] != "containers":
                        continue
                    page = archive.get(key, "containers", state["remote"])
                    if page is not None:
                        items = [
                            record if x.get("id") == record["id"] else x
                            for x in page.get("items", [])
                        ]
                        if not state["remote"] and not any(
                            x.get("id") == record["id"] for x in items
                        ):
                            items.insert(0, record)
                        archive.put(key, "containers", state["remote"], dict(page, items=items))
                archive.mark_ready(key, "container", project.origin.remote_id)
            except OSError:
                record["cache_warning"] = "Proje güncellendi ancak yerel arşiv kaydı tamamlanamadı"
        return record

    def change_project(self, key, value, operation="update", **values):
        context, remote = origin(value, key)
        if context != key or not remote:
            raise ValueError("Proje seçili hesaba ait olmalı")
        provider = self.provider(key)
        if operation == "update":
            current = self.call(provider.get_project(remote))
            result = self.call(provider.update_project(replace(current, **values)), timeout=90)
        elif operation == "add_source":
            result = self.call(provider.add_project_resource(remote, **values), timeout=90)
        elif operation == "remove_source":
            result = self.call(provider.remove_project_resource(remote, **values), timeout=90)
        else:
            raise ValueError("Bilinmeyen proje işlemi")
        return self.store_project(key, result)

    def delete(self, value, key, kind="conversation"):
        context, remote = origin(value, key)
        if context != key or kind not in ("conversation", "container") or not remote:
            raise ValueError("Silinecek kayıt seçili hesaba ait olmalı")
        with self._lock:
            if self._sending.get(key, 0):
                raise RuntimeError("Hesabın gönderimleri tamamlandıktan sonra silin")
        provider = self.provider(key)
        capability = Capability.DELETE if kind == "conversation" else Capability.PROJECT_DELETE
        if capability not in provider.capabilities:
            raise ValueError("Sağlayıcı bu silme işlemini desteklemiyor")

        confirmed_children = None

        def cleanup():
            archive = self.archive()
            try:
                removed = (
                    archive.delete(key, kind, remote, children=confirmed_children)
                    if archive
                    else [(key, kind, remote)]
                )
                if kind == "container":
                    for member_key, project_id in tuple(self._chat_projects.items()):
                        if member_key[0] == key and project_id == remote:
                            self._chat_projects.pop(member_key, None)
                for _, target_kind, target in removed:
                    if target_kind == "conversation":
                        pointer = archive.get(key, "pointer", "active") if archive else None
                        if isinstance(pointer, dict) and pointer.get("remote") == target:
                            self.set_active(key, None)
                        if archive:
                            self.repository.cache_delete(key, "conversation", target)
                            self.repository.cache_delete(key, "tombstone", target)
                        else:
                            self.repository.cache_mark_deleted(key, target)
                        legacy = self.cache(key)
                        legacy.conversation_cache_path(target).unlink(missing_ok=True)
                        legacy.conversation_cache_path(target).with_suffix(".deleted").unlink(
                            missing_ok=True
                        )
                        if legacy.active_conversation_id() == target:
                            legacy.set_active_conversation(None)
                        self._chat_projects.pop((key, target), None)
            except OSError as error:
                raise RuntimeError(
                    "Sağlayıcıdan silindi; yerel kayıt temizlenemedi. Yenileyin."
                ) from error
            return {"removed": [identity(a, r) for a, _, r in removed], "kind": kind}

        async def remove():
            nonlocal confirmed_children
            archive = self.archive()
            members = (
                await asyncio.to_thread(archive.repository.container_members, key, remote)
                if kind == "container" and archive
                else set()
            )
            gate = self.api.tasks.account_gate(*key.split("/", 1))
            async with gate:
                try:
                    await (
                        provider.delete_chat(remote)
                        if kind == "conversation"
                        else provider.delete_project(remote)
                    )
                except RemoteNotFound:
                    # A missing mutation route is not proof that the record is gone.
                    try:
                        await (
                            provider.get_chat(remote)
                            if kind == "conversation"
                            else provider.get_project(remote)
                        )
                    except RemoteNotFound:
                        pass
                    else:
                        raise ProviderError(
                            "Sağlayıcı silmeyi doğrulamadı; uzak ve yerel kayıt korundu"
                        )
                if kind == "container":
                    confirmed_children = set()
                    for child in members:
                        try:
                            chat = await provider.get_chat(child)
                            if chat.metadata.get("is_visible") is False:
                                confirmed_children.add(child)
                        except RemoteNotFound:
                            confirmed_children.add(child)
                        except ProviderError:
                            pass  # Unknown remote outcome must not hide a surviving chat.
                return await asyncio.to_thread(cleanup)

        return self.call(remove(), timeout=60)

    def branch_chat(self, key, conversation_id, message_id):
        context, remote = origin(conversation_id, key)
        if context != key or not remote:
            raise ValueError("The conversation belongs to a different account")
        if not self.enabled(key.split("/")[0]):
            raise ValueError("Provider is disabled")
        if key not in {item["key"] for item in self.active_accounts()}:
            raise ValueError("Select this account before creating a branch")
        archive = self.archive()
        if archive and archive.deleted(key, "conversation", remote):
            raise RemoteNotFound("The conversation was deleted")
        source = self.cached_window(key, remote) or {}
        with self._lock:
            if key in self._session_changes:
                raise RuntimeError("Account verification is still running")
            self._sending[key] = self._sending.get(key, 0) + 1

        async def branch():
            provider = self.provider(key)
            async with self.api.tasks.account_gate(provider.id, provider.account_id):
                if not self.enabled(provider.id):
                    raise ValueError("Provider is disabled")
                if archive and archive.deleted(key, "conversation", remote):
                    raise RemoteNotFound("The conversation was deleted")
                return await provider.branch_chat(remote, message_id)

        try:
            record = self.chat_record(key, self.call(branch()))
            if record["remote_id"] == remote:
                raise ProviderError("The provider did not create a separate conversation")
            if record.get("metadata", {}).get("title_unavailable") or not str(
                record.get("title") or ""
            ).strip():
                record["title"] = source.get("title") or record.get("title") or "Untitled"
            if not record.get("project_id"):
                record["project_id"] = source.get("project_id")
            archive = self.archive()
            native_preview = isinstance(record.get("pending_branch"), dict)
            if native_preview:
                record["branch_unanswered"] = True
                if not archive:
                    record["pending_local_branch"] = True
            self.cache_conversation(key, record, make_active=False)
            if archive and native_preview:
                archive.mark_pending_branch(key, record["remote_id"])
            return record
        finally:
            with self._lock:
                self._sending[key] -= 1

    def callbacks(self, key):
        if not key:

            def unavailable(*args, **kwargs):
                raise ValueError("Önce hesap ekleyin")

            return {
                "load_conversation": unavailable,
                "send_prompt": unavailable,
                "load_catalog": None,
                "list_projects_callback": None,
                "get_project_callback": None,
                "list_project_chats_callback": None,
                "upload_image": None,
                "move_conversation": None,
                "rename_conversation": None,
                "session_action": lambda action: (
                    self.status("") if action == "status" else unavailable()
                ),
                "forget_temporary": None,
                "attach_file_callback": None,
                "attachment_policy_callback": None,
            }
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

            page = self.read_record(
                context,
                "container_chats",
                remote + ":" + (cursor or ""),
                fetch,
                fresh=True,
            )
            return self._include_pending_project_branches(context, remote, page, cursor)

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
                result = provider.forget_temporary(remote)
                if asyncio.iscoroutine(result):
                    self.call(result)
            elif hasattr(provider, "_temporary_conversations"):
                provider._temporary_conversations.pop(remote, None)

        return {
            "upload_image": lambda *args: self.upload_image(key, *args),
            "load_conversation": lambda value: self.load(value, key, window=True),
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
            "attachment_policy_callback": lambda: self.attachment_policy(key),
        }

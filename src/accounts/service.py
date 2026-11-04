"""Account service coordination and lifecycle."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from concurrent.futures import CancelledError, Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import asdict, replace

try:
    from builtins import ExceptionGroup
except ImportError:  # Python 3.10
    from exceptiongroup import ExceptionGroup

from providers.chatgpt import BrowserSession, SessionPaths
from providers.errors import AccountPaused
from providers.models import FeatureState, ModelCatalog
from providers.registry import PROVIDERS
from providers.session import read_session, session_exists

from .history import AccountsHistory
from .projects import AccountsProjects
from .repository import AccountRepository
from .sending import AccountsSending
from .sessions import AccountsSessions
from .shared import (
    _LOG,
    CATALOG_SCHEMA,
    MODEL_TIMEOUT,
    identity,
    origin,
)
from .transfers import AccountsTransfers


class Accounts(
    AccountsSessions,
    AccountsHistory,
    AccountsProjects,
    AccountsSending,
    AccountsTransfers,
):
    """Coordinate account-scoped provider operations."""

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
        self._verification_required = set()
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
        self._active_calls = set()
        self._active_pending = {}
        self._active_values = {}
        self._active_writer_running = False
        self._active_writer_task = None
        self._legacy_cache_migration_lock = threading.Lock()
        self._legacy_cache_migration_complete = False
        for item in self.items():
            key = item["key"]
            try:
                marker = self.repository.get(key, "verification")
                if marker and marker.get("requires_verification") is True:
                    self._verification_required.add(key)
            except (OSError, ValueError, AttributeError):
                pass
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
                account_enabled=item.get("enabled", True),
                provider_enabled=config["providers"]
                .get(item["provider"], {})
                .get("enabled", False),
                enabled=item.get("enabled", True)
                and config["providers"].get(item["provider"], {}).get("enabled", False),
            )
            for item in config["accounts"]["items"]
        ]

    def visible_accounts(self):
        return [
            item
            for item in self.items()
            if item["enabled"] and item["key"] not in self._verification_required
        ]

    def account_is_visible(self, key):
        try:
            item = self.item(key)
        except ValueError:
            return False
        return item["enabled"] and key not in self._verification_required

    def require_visible_account(self, key):
        item = self.item(key)
        if not item["enabled"] or key in self._verification_required:
            raise ValueError("Hesap devre dışı veya yeniden doğrulanmalı")
        return item

    def enabled(self, provider):
        return self.settings.snapshot()["providers"].get(provider, {}).get("enabled", False)

    def set_enabled(self, provider, enabled):
        with self._lock:
            if not enabled and any(
                count and key.split("/")[0] == provider for key, count in self._sending.items()
            ):
                raise ValueError("Önce bu sağlayıcıdaki yanıtları tamamlayın veya durdurun")
            self.settings.configure_provider(provider, enabled)

    def set_account_enabled(self, key, enabled):
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        self.item(key)
        with self._lock:
            if self._sending.get(key, 0) or key in self._session_changes:
                raise RuntimeError("Önce devam eden işlemi tamamlayın veya durdurun")
            self._session_changes.add(key)
        try:
            with self._catalog_io_lock:
                self.settings.set_account_enabled(key, enabled)
                if not enabled:
                    self._catalog_versions[key] = self._catalog_versions.get(key, 0) + 1
                    self._catalogs.pop(key, None)
            if not enabled:
                task = self._catalog_tasks.get(key)
                if task and self._loop is not None and self._loop.is_running():
                    self._loop.call_soon_threadsafe(task.cancel)
        finally:
            with self._lock:
                self._session_changes.discard(key)
        return self.item(key)

    def preference(self, key):
        return self.settings.snapshot()["accounts"].get("preferences", {}).get(key, {})

    def remember(self, key, model, effort):
        self.item(key)
        self.settings.update("accounts", {"preferences": {key: {"model": model, "effort": effort}}})

    def active_accounts(self):
        if self.closing or self.closed:
            return []
        data = self.settings.snapshot()["accounts"]
        legacy = data["active"]
        grouped = {}
        for item in self.visible_accounts():
            grouped.setdefault(item["provider"], []).append(item)
        selected = data.get("selected", {})
        active = []
        for provider, items in grouped.items():
            keys = {item["key"] for item in items}
            key = selected.get(provider)
            if key not in keys:
                key = (
                    legacy
                    if legacy.startswith(provider + "/") and legacy in keys
                    else items[0]["key"]
                )
            active.append(next(item for item in items if item["key"] == key))
        return active

    def archive(self):
        if self.closed:
            return None
        try:
            archive = self.api.get("archive")
            return None if getattr(archive, "closed", False) is True else archive
        except LookupError:
            return None

    def read_record(
        self, key, kind, remote, fetch, force=False, *, window=False, fresh=False, window_limit=None
    ):
        archive = self.archive()
        if archive:
            try:
                options = {"window": True} if window else {}
                if fresh:
                    options["fresh"] = True
                if window_limit is not None:
                    options["window_limit"] = window_limit
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
            "enabled": True,
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
        selected = dict(self.settings.snapshot()["accounts"].get("selected", {}))
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
            SessionPaths(self.credentials(key), directory / "chrome-profile"),
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

        started = threading.Event()

        async def bounded():
            started.set()
            if _closing:
                # Keep the shutdown coroutine in the run_coroutine_threadsafe
                # task. wait_for() wraps it in a child task; Accounts.close()
                # then mistakes this child/parent pair for background work
                # and cancels its own caller while draining the provider loop.
                return await coroutine
            return await asyncio.wait_for(coroutine, timeout)

        with self._lock:
            if (self.closing or self.closed) and not _closing:
                coroutine.close()
                raise RuntimeError("Hesap hizmeti kapalı")
            future = asyncio.run_coroutine_threadsafe(bounded(), self._loop)
            if not _closing:
                self._active_calls.add(future)
        if not _closing:
            future.add_done_callback(self._forget_active_call)
        try:
            return future.result(timeout=timeout + 1)
        except (TimeoutError, asyncio.TimeoutError, FutureTimeout):
            future.cancel()
            if not started.is_set():
                coroutine.close()
            raise TimeoutError(
                "Sağlayıcı işlemi zaman aşımına uğradı; yeniden deneyebilirsiniz"
            ) from None
        except CancelledError:
            if not started.is_set():
                coroutine.close()
            raise

    def _forget_active_call(self, future):
        with self._lock:
            self._active_calls.discard(future)

    def begin_shutdown(self):
        """Stop new provider work and release threads waiting on provider calls."""
        with self._lock:
            if self.closed:
                return
            self.closing = True
            loop = self._loop
            calls = tuple(self._active_calls)
        for future in calls:
            future.cancel()
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(self.api.tasks.begin_shutdown)
            except RuntimeError:
                pass

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

    def prepare_enabled(self, selected_key=None):
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
            for item in self.visible_accounts()
            if session_exists(self.credentials(item["key"]))
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
                prepare_for_send = getattr(provider, "prepare_for_send", None)
                if key == selected_key and callable(prepare_for_send):
                    self.call(prepare_for_send(), MODEL_TIMEOUT)
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
        self.require_visible_account(key)

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
        self.begin_shutdown()
        with self._lock:
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
                "release_conversation": lambda chat: None,
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
            context, remote = origin(chat, key)
            target = self.provider(context)
            self.call(target.rename_chat(remote, title))
            archive = self.archive()
            if archive:
                archive.rename_conversation(context, remote, title)
            cached = self.repository.cache_get(context, "conversation", remote)
            if isinstance(cached, dict):
                cached["title"] = title
                self.repository.cache_put(context, "conversation", remote, cached)

        def forget(chat):
            _, remote = origin(chat, key)
            if hasattr(provider, "forget_temporary"):
                result = provider.forget_temporary(remote)
                if asyncio.iscoroutine(result):
                    self.call(result)
            elif hasattr(provider, "_temporary_conversations"):
                provider._temporary_conversations.pop(remote, None)

        def release_conversation(chat):
            _, remote = origin(chat, key)
            if hasattr(provider, "forget_conversation"):
                result = provider.forget_conversation(remote)
                if asyncio.iscoroutine(result):
                    self.call(result)

        return {
            "upload_image": lambda *args: self.upload_image(key, *args),
            "load_conversation": lambda value: self.load(value, key, window=True, window_limit=25),
            "send_prompt": lambda *args, **kwargs: self.send(key, *args, **kwargs),
            "load_catalog": lambda: self.models(key),
            "list_projects_callback": lambda cursor=None: self.list_projects(key, cursor),
            "get_project_callback": get_project,
            "list_project_chats_callback": project_chats,
            "move_conversation": move,
            "rename_conversation": rename,
            "session_action": lambda action: self.session_action(key, action),
            "forget_temporary": forget,
            "release_conversation": release_conversation,
            "attach_file_callback": lambda path: self.attach_file(key, path),
            "attachment_policy_callback": lambda: self.attachment_policy(key),
        }

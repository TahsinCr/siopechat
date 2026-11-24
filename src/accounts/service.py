"""Account service coordination and native synchronous v2 provider lifecycle."""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

try:
    from builtins import ExceptionGroup
except ImportError:  # Python 3.10
    from exceptiongroup import ExceptionGroup

from webaiprovider import (
    AccountPaused,
    Capability,
    ChatGPTWebAIProvider,
    EventEnvelope,
    LifecycleState,
    Project,
    WebAIAccount,
    WebAIProvider,
    WebAIProviderType,
)
from webaiprovider.accounts import session_exists
from webaiprovider.providers.chatgpt.auth import BrowserSession

from .history import AccountsHistory
from .projects import AccountsProjects
from .repository import AccountRepository
from .sending import AccountsSending
from .sessions import AccountsSessions
from .shared import (
    CATALOG_SCHEMA,
    PROVIDERS,
    AccountCatalog,
    origin,
)
from .transfers import AccountsTransfers

_MAX_CACHED_CHATS = 128


class Accounts(
    AccountsSessions,
    AccountsHistory,
    AccountsProjects,
    AccountsSending,
    AccountsTransfers,
):
    """Coordinate account-scoped synchronous v2 provider operations."""

    def __init__(self, api):
        self.api = api
        self.settings = api.settings
        self.closing = False
        self.closed = False
        self._chat_projects: dict[tuple[str, str], str] = {}
        self.data_root = api.get("data_root")
        self.runtime_root = api.get("runtime_root")
        self.repository = AccountRepository(self.data_root / "accounts.sqlite3")
        self._providers: dict[str, WebAIProvider] = {}
        self._accounts: dict[str, WebAIAccount] = {}
        self._event_subscriptions: dict[str, object] = {}
        self._provider_locks: dict[str, threading.RLock] = {}
        self._chats: OrderedDict[tuple[str, str], object] = OrderedDict()
        self._credential_stores: dict[str, object] = {}
        self._credential_lock = threading.Lock()
        self.cdp_url = None
        self._verified: set[str] = set()
        self._verification_required: set[str] = set()
        self._catalogs: dict[str, tuple[int, AccountCatalog]] = {}
        self._catalog_versions: dict[str, int] = {}
        self._catalog_io_lock = threading.Lock()
        self._sending: dict[str, int] = {}
        self._preparing_chats: set[tuple[str, str | None, str | None]] = set()
        self._active_operations: set[object] = set()
        self._send_locks: dict[str, threading.RLock] = {}
        self._session_changes: set[str] = set()
        self._attachment_paths: dict[tuple[str, str], str] = {}
        self._lock = threading.RLock()
        self._active_pending: dict[str, str | None] = {}
        self._active_values: dict[str, str | None] = {}
        self._active_writer_running = False
        self._active_writer_thread = None
        self._paused_accounts: set[tuple[str, str]] = set()
        for item in self.items():
            key = item["key"]
            try:
                marker = self.repository.get(key, "verification")
                if marker and marker.get("requires_verification") is True:
                    self._verification_required.add(key)
            except (OSError, ValueError, AttributeError):
                pass

    def _provider_lock(self, key: str) -> threading.RLock:
        with self._lock:
            lock = self._provider_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._provider_locks[key] = lock
            return lock

    def _account_gate(self, key: str) -> threading.RLock:
        with self._lock:
            lock = self._send_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._send_locks[key] = lock
            return lock

    def _cdp_url_for(self, key: str) -> str:
        item = self.item(key)
        if item.get("browser_account"):
            return self._cdp_url_for(item["browser_account"])
        if key == "chatgpt/default" and self.cdp_url:
            return self.cdp_url
        return f"http://127.0.0.1:{item['port']}"

    def _account_obj(self, key: str) -> WebAIAccount:
        item = self.item(key)
        account = self._accounts.get(key)
        if account is not None and account.name == item["label"]:
            return account
        account = WebAIAccount(
            id=item["id"],
            provider=item["provider"],
            credentials=self.credentials(key),
            name=item["label"],
        )
        self._accounts[key] = account
        return account

    def _build_provider(self, key: str, account: WebAIAccount | None = None) -> WebAIProvider:
        item = self.item(key)
        resolved_account = account if account is not None else self._account_obj(key)
        provider_id = item["provider"]
        if provider_id == "chatgpt":
            session = BrowserSession(self.credentials(key), cdp_url=self._cdp_url_for(key))
            return ChatGPTWebAIProvider(resolved_account, session=session)
        return WebAIProvider(resolved_account, type=WebAIProviderType.parse(provider_id))

    def _subscribe_provider(self, key: str, prov: WebAIProvider) -> None:
        previous = self._event_subscriptions.pop(key, None)
        if previous is not None and hasattr(previous, "cancel"):
            previous.cancel()
        if hasattr(prov, "events") and hasattr(prov.events, "subscribe"):
            self._event_subscriptions[key] = prov.events.subscribe(
                lambda envelope, account_key=key: self._on_provider_event(account_key, envelope)
            )

    def _on_provider_event(self, key: str, envelope: EventEnvelope | object) -> None:
        name = getattr(envelope, "name", None)
        if name == "provider.opened":
            self._remember_verification(key)
            return
        if name not in ("collection.changed", "chat.created", "chat.updated", "chat.deleted"):
            return
        payload = getattr(envelope, "payload", None)
        archive = self.archive()
        if name == "chat.deleted" and archive:
            rid = getattr(envelope, "resource_id", None)
            if isinstance(rid, str) and rid:
                self._forget_chat_handle(key, rid)
        elif name == "collection.changed" and isinstance(payload, dict):
            collection = payload.get("collection")
            action = payload.get("action")
            item = payload.get("item")
            if collection == "chats" and action == "update" and item is not None:
                remote_id = getattr(item, "remote_id", None)
                title = getattr(item, "title", None)
                if archive and isinstance(remote_id, str) and isinstance(title, str) and title:
                    archive.rename_conversation(key, remote_id, title)

    def _forget_chat_handle(self, key: str, remote_id: str | None = None) -> None:
        with self._lock:
            if remote_id is None:
                for entry_key in tuple(self._chats):
                    if entry_key[0] == key:
                        self._chats.pop(entry_key, None)
            else:
                self._chats.pop((key, remote_id), None)

    def _chat_handle(
        self,
        key: str,
        remote_id: str | None = None,
        *,
        project_id: str | None = None,
        temporary: bool = False,
        create_new: bool = False,
    ):
        prov = self.open_provider(key)
        if create_new or not remote_id:
            project = prov.projects.get(project_id) if project_id else None
            return prov.chats.create(project=project, temporary=temporary)
        with self._lock:
            existing = self._chats.get((key, remote_id))
            if existing is not None and getattr(existing, "provider", None) is prov:
                self._chats.move_to_end((key, remote_id))
                return existing
        chat = prov.chats._new(
            id=remote_id,
            remote_id=remote_id,
            temporary=temporary,
        )
        if project_id:
            chat.project = Project(id=project_id, remote_id=project_id, provider=prov)
        self._cache_chat_handle(key, chat)
        return chat

    def _cache_chat_handle(self, key: str, chat: object) -> None:
        remote_id = getattr(chat, "remote_id", None)
        if not isinstance(remote_id, str) or not remote_id or getattr(chat, "temporary", False):
            return
        with self._lock:
            self._chats[key, remote_id] = chat
            self._chats.move_to_end((key, remote_id))
            while len(self._chats) > _MAX_CACHED_CHATS:
                self._chats.popitem(last=False)

    def _discard_provider(self, key: str) -> None:
        with self._provider_lock(key):
            sub = self._event_subscriptions.pop(key, None)
            if sub is not None and hasattr(sub, "cancel"):
                sub.cancel()
            prov = self._providers.pop(key, None)
            self._forget_chat_handle(key)
        if prov is not None:
            prov.close()

    def items(self, config=None):
        if config is None:
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

    def visible_accounts(self, config=None):
        return [
            item
            for item in self.items(config=config)
            if item["enabled"] and item["key"] not in self._verification_required
        ]

    def account_is_visible(self, key: str) -> bool:
        try:
            item = self.item(key)
        except ValueError:
            return False
        return item["enabled"] and key not in self._verification_required

    def require_visible_account(self, key: str):
        item = self.item(key)
        if not item["enabled"] or key in self._verification_required:
            raise ValueError("Hesap devre dışı veya yeniden doğrulanmalı")
        return item

    def enabled(self, provider: str) -> bool:
        return self.settings.snapshot()["providers"].get(provider, {}).get("enabled", False)

    def set_enabled(self, provider: str, enabled: bool) -> None:
        with self._lock:
            if not enabled and any(
                count and key.split("/")[0] == provider for key, count in self._sending.items()
            ):
                raise ValueError("Önce bu sağlayıcıdaki yanıtları tamamlayın veya durdurun")
            self.settings.configure_provider(provider, enabled)

    def set_account_enabled(self, key: str, enabled: bool):
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
                self._discard_provider(key)
        finally:
            with self._lock:
                self._session_changes.discard(key)
        return self.item(key)

    def preference(self, key: str):
        return self.settings.snapshot()["accounts"].get("preferences", {}).get(key, {})

    def remember(self, key: str, model: str, effort: str | None) -> None:
        self.item(key)
        self.settings.update("accounts", {"preferences": {key: {"model": model, "effort": effort}}})

    def active_accounts(self, config=None):
        if self.closing or self.closed:
            return []
        if config is None:
            config = self.settings.snapshot()
        data = config["accounts"]
        legacy = data["active"]
        grouped = {}
        for item in self.visible_accounts(config=config):
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
        if archive is None:
            return fetch()
        options = {"window": True} if window else {}
        if fresh:
            options["fresh"] = True
        if window_limit is not None:
            options["window_limit"] = window_limit
        return archive.read(key, kind, remote or "", fetch, force=force, **options)

    def active(self) -> str:
        config = self.settings.snapshot()
        key = config["accounts"]["active"]
        if key and config["providers"].get(key.split("/")[0], {}).get("enabled", False):
            return key
        active = self.active_accounts(config=config)
        return active[0]["key"] if active else ""

    def item(self, key: str):
        item = next((item for item in self.items() if item["key"] == key), None)
        if item is None:
            raise ValueError("Hesap bulunamadı")
        return item

    def add(self, provider: str, label: str, browser_account=None) -> str:
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

    def select(self, key: str) -> None:
        self.item(key)
        if not self.enabled(key.split("/")[0]):
            raise ValueError("Sağlayıcı devre dışı; önce Ayarlar → Sağlayıcılar bölümünden açın")
        selected = dict(self.settings.snapshot()["accounts"].get("selected", {}))
        selected[key.split("/")[0]] = key
        self.settings.update("accounts", {"active": key, "selected": selected})

    def rename(self, key: str, label: str):
        """Rename the local display label while keeping the provider session intact."""
        self.settings.rename_account(key, label)
        self._accounts.pop(key, None)
        return self.item(key)

    def remove(self, key: str):
        """Forget a local account; never delete the remote account or archived conversations."""
        self.item(key)
        with self._lock:
            if self._sending.get(key, 0) or key in self._session_changes:
                raise RuntimeError("Önce devam eden işlemi tamamlayın veya durdurun")
            self._session_changes.add(key)
        try:
            if not any(item["key"] != key and item["enabled"] for item in self.items()):
                raise ValueError("Son etkin hesap silinemez; önce başka bir hesap ekleyin")

            self._discard_provider(key)
            previous_accounts = self.settings.snapshot()["accounts"]
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
            self._accounts.pop(key, None)
            with self._credential_lock:
                self._credential_stores.pop(key, None)
            self._verified.discard(key)
            self._verification_required.discard(key)
            with self._lock:
                self._active_pending.pop(key, None)
                self._active_values.pop(key, None)
            self._chat_projects = {k: v for k, v in self._chat_projects.items() if k[0] != key}
            return {"removed": key, "active_account": active}
        finally:
            with self._lock:
                self._session_changes.discard(key)

    def directory(self, key: str):
        self.item(key)
        return self.data_root / "accounts" / key

    def session(self, key: str) -> BrowserSession:
        return BrowserSession(self.credentials(key), cdp_url=self._cdp_url_for(key))

    def credentials(self, key: str):
        with self._credential_lock:
            existing = self._credential_stores.get(key)
            if existing is None:
                existing = self.repository.credentials(key)
                self._credential_stores[key] = existing
            return existing

    def capabilities(self, key: str) -> frozenset[Capability]:
        return self.provider(key).capabilities

    def provider(self, key: str) -> WebAIProvider:
        with self._provider_lock(key):
            if self.closing or self.closed:
                raise RuntimeError("Hesap hizmeti kapalı")
            prov = self._providers.get(key)
            if prov is None or prov.lifecycle_state in (
                LifecycleState.CLOSED,
                LifecycleState.CLOSING,
                LifecycleState.CLOSE_FAILED,
            ):
                prov = self._build_provider(key)
                self._subscribe_provider(key, prov)
                self._providers[key] = prov
            return prov

    def open_provider(self, key: str) -> WebAIProvider:
        with self._provider_lock(key):
            if self.closing or self.closed:
                raise RuntimeError("Hesap hizmeti kapalı")
            prov = self.provider(key)
            if not prov.opened:
                try:
                    prov.open()
                except AccountPaused as error:
                    if getattr(error, "requires_verification", True) and session_exists(
                        self.credentials(key)
                    ):
                        self._forget_verification(key)
                    raise
                except Exception:
                    if prov.lifecycle_state in (LifecycleState.OPEN_FAILED, LifecycleState.CLOSED):
                        with self._catalog_io_lock:
                            self._providers.pop(key, None)
                    raise
            return prov

    def begin_shutdown(self) -> None:
        """Stop new provider work and cancel active synchronous operations."""
        with self._lock:
            if self.closed:
                return
            self.closing = True
            ops = tuple(self._active_operations)
        for operation in ops:
            try:
                operation.cancel()
            except Exception:
                pass

    def cached_models(self, key: str) -> AccountCatalog | None:
        cached = self._catalogs.get(key)
        if cached:
            return cached[1]
        try:
            record = self.repository.get(key, "catalog")
            if record is None:
                return None
            if record.get("account_key") != key or record.get("schema") != CATALOG_SCHEMA:
                return None
            catalog = AccountCatalog.from_dict(record["catalog"])
            if catalog.state.value != "available":
                return None
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
        with self._catalog_io_lock:
            self._catalogs.setdefault(key, (self._catalog_versions.get(key, 0), catalog))
            return self._catalogs[key][1]

    def _store_catalog(self, key: str, version: int, refs) -> AccountCatalog:
        catalog = AccountCatalog.from_model_refs(refs)
        with self._catalog_io_lock:
            if version == self._catalog_versions.get(key, 0):
                self._catalogs[key] = (version, catalog)
                try:
                    self.repository.put(
                        key,
                        "catalog",
                        {
                            "schema": CATALOG_SCHEMA,
                            "account_key": key,
                            "catalog": catalog.to_dict(),
                        },
                    )
                except OSError:
                    pass
        return catalog

    def prepare_enabled(self, selected_key: str | None = None) -> dict[str, str]:
        """Warm saved sessions and model catalogs in parallel for enabled accounts."""
        accounts = [
            item
            for item in self.visible_accounts()
            if session_exists(self.credentials(item["key"]))
        ]
        if not accounts:
            return {}

        def prepare(item):
            if self.closing or self.closed:
                return item["key"], None
            key = item["key"]
            version = self._catalog_versions.get(key, 0)
            try:
                prov = self.open_provider(key)
                self._store_catalog(key, version, prov.models.list())
                if key == selected_key:
                    active_remote = self.active_conversation(key)
                    if active_remote:
                        try:
                            self.prepare_chat(key, active_remote)
                        except Exception:
                            pass
            except AccountPaused as error:
                if getattr(error, "requires_verification", True) and session_exists(
                    self.credentials(key)
                ):
                    self._forget_verification(key)
                return key, str(error)
            except Exception as error:
                return key, str(error)
            return key, None

        with ThreadPoolExecutor(max_workers=min(4, len(accounts))) as pool:
            results = pool.map(prepare, accounts)
            return {key: error for key, error in results if error}

    def models(self, key: str, force: bool = False) -> AccountCatalog:
        self.require_visible_account(key)
        if not force:
            cached = self.cached_models(key)
            if cached is not None:
                return cached
        with self._provider_lock(key):
            if not force:
                cached = self.cached_models(key)
                if cached is not None:
                    return cached
            version = self._catalog_versions.get(key, 0)
            try:
                prov = self.open_provider(key)
                refs = prov.models.refresh() if force else prov.models.list()
            except AccountPaused as error:
                if getattr(error, "requires_verification", True) and session_exists(
                    self.credentials(key)
                ):
                    self._forget_verification(key)
                raise
            self._remember_verification(key)
            return self._store_catalog(key, version, refs)

    def close(self) -> None:
        with self._lock:
            if self.closed:
                return
        self.begin_shutdown()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            with self._lock:
                if not any(self._sending.values()):
                    break
            time.sleep(0.005)
        thread = self._active_writer_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
        errors = []
        for sub in list(self._event_subscriptions.values()):
            if hasattr(sub, "cancel"):
                sub.cancel()
        self._event_subscriptions.clear()
        provider_keys = list(self._providers)
        if len(provider_keys) == 1:
            try:
                self._discard_provider(provider_keys[0])
            except Exception as error:
                errors.append(error)
        elif provider_keys:

            def _discard_safe(key: str):
                try:
                    self._discard_provider(key)
                    return None
                except Exception as error:
                    return error

            with ThreadPoolExecutor(max_workers=min(4, len(provider_keys))) as pool:
                for error in pool.map(_discard_safe, provider_keys):
                    if error is not None:
                        errors.append(error)
        with self._lock:
            self.closed = True
        if errors:
            if len(errors) == 1:
                raise errors[0]
            raise ExceptionGroup("account shutdown failed", errors)

    def callbacks(self, key: str):
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

        def get_project(project):
            context, remote = origin(project, key)
            return self.read_record(
                context,
                "container",
                remote,
                lambda: self.project_record(
                    context, self.open_provider(context).projects.get(remote)
                ),
            )

        def project_chats(project, cursor=None):
            context, remote = origin(project, key)

            def fetch():
                proj = self.open_provider(context).projects.get(remote)
                page = proj.chats._fetch_page(cursor, None)
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

        def move(chat_id, project_id):
            from .shared import origin

            chat_context, remote = origin(chat_id, key)
            project_context, project_remote = origin(project_id, key)
            if chat_context != key or project_context != key:
                raise ValueError("Sohbet ve proje aynı sağlayıcı hesabında olmalı")
            self.open_provider(key)
            chat = self._chat_handle(key, remote)
            if project_remote is None and hasattr(chat, "_move_to"):
                return chat._move_to(None, allow_reassignment=True)
            return chat.move_to(project_remote)

        def rename(chat_id, title):
            from .shared import origin

            context, remote = origin(chat_id, key)
            self.open_provider(context)
            chat = self._chat_handle(context, remote)
            chat.rename(title)
            archive = self.archive()
            if archive:
                archive.rename_conversation(context, remote, title)

        def forget(chat_id):
            from .shared import origin

            _, remote = origin(chat_id, key)
            self._forget_chat_handle(key, remote)
            prov = self._providers.get(key)
            if prov is None:
                return
            if hasattr(prov, "_temporary_conversations"):
                prov._temporary_conversations.pop(remote, None)
            if hasattr(prov, "_temporary"):
                prov._temporary.pop(remote, None)

        def release_conversation(chat_id):
            from .shared import origin

            _, remote = origin(chat_id, key)
            self._forget_chat_handle(key, remote)
            prov = self._providers.get(key)
            if prov is not None and hasattr(prov, "_pending_branches"):
                prov._pending_branches.pop(remote, None)

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

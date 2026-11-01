"""AccountsHistory account operations."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict

from backend import BackendService
from providers import (
    Capability,
)
from providers.errors import RemoteNotFound

from .shared import (
    HISTORY_TIMEOUT,
    _shallow_dataclass,
    identity,
    origin,
)


class AccountsHistory:
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

    def cached_window(self, key, remote=None, *, limit=None):
        """UI history reads use bounded SQL windows; other API callers retain full history."""
        archive = self.archive()
        remote = remote or self.active_conversation(key)
        if not remote:
            return None
        if archive:
            kw = {"limit": limit} if limit is not None else {}
            value = archive.conversation_window(key, remote, **kw)
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
            item.get("remote_id") or item.get("id") for item in listed if isinstance(item, dict)
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
        return BackendService(self.directory(key))

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

        def content_record(content):
            return dict(
                _shallow_dataclass(content),
                resource=(
                    _shallow_dataclass(content.resource) if content.resource is not None else None
                ),
            )

        def message_record(message):
            value = {
                "id": message.id,
                "role": message.role,
                "channel": message.channel,
                "text": "\n".join(content.text or "" for content in message.content),
                "content": [content_record(content) for content in message.content],
                "metadata": message.metadata,
                "actions": list(message.actions),
            }
            return value

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
            "messages": [message_record(message) for message in chat.messages],
        }

    def list_chats(
        self,
        selected=None,
        cursor=None,
        force=False,
        account_keys=None,
        on_account_page=None,
    ):
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
            return page if page is not None else fetch()

        def unlisted_pending(items, pending):
            known = {
                item.get("remote_id") or item.get("id") for item in items if isinstance(item, dict)
            }
            return [
                item for item in pending if (item.get("remote_id") or item.get("id")) not in known
            ]

        from concurrent.futures import ThreadPoolExecutor, as_completed

        futures_map = {}
        pages_map = {}
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(accounts)))) as pool:
            for account in accounts:
                fut = pool.submit(page_for, account)
                futures_map[fut] = account
            for future in as_completed(futures_map):
                acc = futures_map[future]
                try:
                    res = future.result()
                except Exception as error:
                    res = error
                pages_map[acc["key"]] = res
                if on_account_page is not None and isinstance(res, dict):
                    delivered = dict(res)
                    pending = self._pending_local_branches(acc["key"], res.get("items", ()))
                    if pending and (not cursors or not cursors.get(acc["key"])):
                        extra = unlisted_pending(res.get("items", ()), pending)
                        if extra:
                            delivered["items"] = list(res.get("items", ())) + extra
                    try:
                        on_account_page(acc, delivered)
                    except Exception:
                        pass
        pages = [pages_map[account["key"]] for account in accounts]
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
                items.extend(unlisted_pending(page["items"], pending))
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

    def load(self, value, key, force=False, *, window=False, window_limit=None):
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

        cached = (
            self.cached_window(key, remote, limit=window_limit)
            if archive
            else self.cached(key, remote)
        )
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
            return self._record_from_chat(key, remote, chat, archive)

        return self.read_record(
            key, "conversation", remote, fetch, force, window=window, window_limit=window_limit
        )

    def _record_from_chat(self, key, remote, chat, archive):
        record = self.chat_record(key, chat)
        previous = self.cached_window(key, remote) or {}
        if chat.metadata.get("title_unavailable"):
            record["title"] = previous.get("title") or record["title"]
        if not record.get("project_id"):
            record["project_id"] = previous.get("project_id")
        if not archive:
            self.cache_conversation(key, dict(record, id=remote), make_active=False)
        return record

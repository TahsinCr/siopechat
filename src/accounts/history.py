"""AccountsHistory account operations backed directly by webaiprovider v2 and Archive."""

from __future__ import annotations

import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import islice
from typing import Any

from webaiprovider import (
    Capability,
    RemoteNotFound,
    Response,
    TextPart,
    ThinkingPart,
    WebAIProviderError,
)

from .shared import (
    _shallow_dataclass,
    identity,
    origin,
)


def _conversation_messages_to_dicts(raw_messages: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in raw_messages:
        if isinstance(item, dict):
            result.append(dict(item))
            continue
        content = [
            dict(_shallow_dataclass(part), resource=_shallow_dataclass(part.resource))
            if getattr(part, "resource", None) is not None
            else _shallow_dataclass(part)
            for part in getattr(item, "content", ())
        ]
        resources = [part["resource"] for part in content if part.get("resource") is not None]
        text = "\n".join(
            part["text"] for part in content if part.get("kind") == "text" and part.get("text")
        )
        result.append(
            {
                "id": item.id,
                "role": item.role,
                "channel": item.channel,
                "text": text,
                "content": content,
                "resources": resources,
                "metadata": item.metadata,
                "actions": list(getattr(item, "actions", ())),
            }
        )
    return result


def _artifact_dict(artifact: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": artifact.id,
        "name": artifact.name,
        "media_type": artifact.mime_type,
        "mime_type": artifact.mime_type,
        "size_bytes": artifact.size,
        "remote_url": artifact.download_url,
        "kind": artifact.kind,
        **artifact.metadata,
    }
    raw_content = getattr(artifact, "content", None)
    if isinstance(raw_content, bytes):
        data["content_b64"] = base64.b64encode(raw_content).decode("ascii")
        if data.get("size_bytes") is None:
            data["size_bytes"] = len(raw_content)
    elif isinstance(raw_content, str):
        data["content"] = raw_content
        if data.get("size_bytes") is None:
            data["size_bytes"] = len(raw_content.encode("utf-8"))
    return data


def _source_dict(source: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": source.id,
        "url": source.url,
        "title": source.title,
        "description": source.snippet,
        "snippet": source.snippet,
        "kind": source.kind,
        **source.metadata,
    }
    cited = getattr(source, "cited", None)
    if cited is not None:
        data["cited"] = cited
    return data


def _model_slug_from(value: Any) -> str | None:
    if isinstance(value, str):
        return value or None
    if value is not None:
        slug = getattr(value, "id", None)
        if isinstance(slug, str) and slug:
            return slug
    return None


def _responses_to_messages(responses: list[Response], *, provider_id: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for resp in responses:
        msg = resp.message
        if msg is not None:
            resources = [
                {
                    "id": att.id,
                    "name": att.name,
                    "media_type": att.media_type,
                    "mime_type": att.mime_type,
                    "size_bytes": att.size,
                    "remote_url": att.remote_url,
                    **att.metadata,
                }
                for att in msg.attachments
            ]
            messages.append(
                {
                    "id": msg.remote_id or msg.id,
                    "role": "user",
                    "channel": "final",
                    "text": msg.content,
                    "content": [{"kind": "text", "text": msg.content}] if msg.content else [],
                    "resources": resources,
                    "metadata": dict(msg.metadata),
                    "actions": list(msg.metadata.get("actions", ())),
                }
            )
        sources = [_source_dict(item) for item in resp.sources]
        files = [_artifact_dict(item) for item in resp.artifacts]
        resp_id = resp.remote_id or resp.id
        model_slug = _model_slug_from(resp.model) or resp.metadata.get("model")
        effort = resp.metadata.get("effort")
        if hasattr(resp.model, "effort") and resp.model.effort is not None:
            effort = resp.model.effort.id
        base_meta: dict[str, Any] = dict(resp.metadata)
        if model_slug and "model" not in base_meta:
            base_meta["model"] = model_slug
        if effort and "effort" not in base_meta:
            base_meta["effort"] = effort
        if sources and "sources" not in base_meta:
            base_meta["sources"] = sources
        if files and "files" not in base_meta:
            base_meta["files"] = files
        actions = list(resp.metadata.get("actions", ()))

        parts = list(resp.parts)
        text_indices = [idx for idx, p in enumerate(parts) if isinstance(p, TextPart)]
        last_text_idx = text_indices[-1] if text_indices else -1
        if not parts and (resp.remote_id or files or sources):
            messages.append(
                {
                    "id": resp_id,
                    "role": "assistant",
                    "channel": "final",
                    "text": "",
                    "content": [],
                    "metadata": base_meta,
                    "actions": actions,
                }
            )
            continue
        for idx, part in enumerate(parts):
            if isinstance(part, ThinkingPart):
                messages.append(
                    {
                        "id": f"{resp_id}:{part.id}",
                        "part_id": part.id,
                        "role": "reasoning",
                        "channel": "analysis",
                        "text": part.text,
                        "content": [],
                        "metadata": {},
                        "actions": [],
                    }
                )
            elif isinstance(part, TextPart):
                is_final_text = idx == last_text_idx
                messages.append(
                    {
                        "id": resp_id if is_final_text else f"{resp_id}:{part.id}",
                        "part_id": part.id,
                        "role": "assistant",
                        "channel": "final",
                        "text": part.text,
                        "content": [],
                        "metadata": dict(base_meta) if is_final_text else {},
                        "actions": actions if is_final_text else [],
                    }
                )
        if last_text_idx == -1 and (files or sources):
            messages.append(
                {
                    "id": resp_id,
                    "role": "assistant",
                    "channel": "final",
                    "text": "",
                    "content": [],
                    "metadata": base_meta,
                    "actions": actions,
                }
            )
    return messages


class AccountsHistory:
    def cached(self, key: str, remote: str | None = None):
        if not key or not self.account_is_visible(key):
            return None
        archive = self.archive()
        if archive is None:
            return None
        if remote is None:
            pointer = archive.get(key, "pointer", "active")
            if pointer is None:
                return None
            remote = pointer.get("remote")
            if remote is None:
                return None
        if archive.deleted(key, "conversation", remote):
            return None
        return archive.get(key, "conversation", remote)

    def active_conversation(self, key: str) -> str | None:
        if not self.account_is_visible(key):
            return None
        archive = self.archive()
        if archive is None:
            return None
        pointer = archive.get(key, "pointer", "active")
        return pointer.get("remote") if pointer is not None else None

    def cached_window(self, key: str, remote: str | None = None, *, limit: int | None = None):
        """UI history reads use bounded SQL windows."""
        if not self.account_is_visible(key):
            return None
        archive = self.archive()
        remote = remote or self.active_conversation(key)
        if not remote or archive is None:
            return None
        kw = {"limit": limit} if limit is not None else {}
        return archive.conversation_window(key, remote, **kw)

    def response_version(self, key: str, conversation_id: str, message_id: str, version: int):
        context, remote = origin(conversation_id, key)
        if context != key or not remote or not self.account_is_visible(key):
            return None
        archive = self.archive()
        return archive.response_version(key, remote, message_id, version) if archive else None

    def _pending_local_branches(self, key: str, listed, *, project_listing: bool = False):
        archive = self.archive()
        if archive is None:
            return []
        remotes = {
            item.get("remote_id") or item.get("id") for item in listed if isinstance(item, dict)
        }
        remotes = {remote for remote in remotes if remote}
        archive.confirm_pending_branches(key, remotes, project=project_listing)
        return [
            item
            for item in archive.pending_branches(key, project=project_listing)
            if isinstance(item.get("pending_branch"), dict)
        ]

    def _include_pending_project_branches(self, key: str, project_remote: str, page, cursor=None):
        if cursor or not isinstance(page, dict):
            return page
        project_identity = identity(key, project_remote)
        items = list(page.get("items") or ())
        known = {
            item.get("remote_id") or item.get("id") for item in items if isinstance(item, dict)
        }
        extra = [
            item
            for item in self._pending_local_branches(key, items, project_listing=True)
            if item.get("project_id") == project_identity
            and (item.get("remote_id") or item.get("id")) not in known
        ]
        return dict(page, items=items + extra) if extra else page

    def cached_project_chats(self, value: str, fallback: str, cursor=None):
        key, remote = origin(value, fallback)
        if not remote or not self.account_is_visible(key):
            return None
        archive = self.archive()
        if archive is None:
            return None
        page = archive.get(key, "container_chats", remote + ":" + (cursor or ""))
        if page is None:
            return None
        return self._include_pending_project_branches(key, remote, page, cursor)

    def queue_active(self, key: str, value: str | None) -> None:
        _, remote = origin(value, key)
        with self._lock:
            previous = self._active_values.get(key)
            self._active_pending[key] = remote
            self._active_values[key] = remote
            if previous == remote and key in self._active_pending:
                return
            self._ensure_active_writer()

    def _ensure_active_writer(self) -> None:
        if self._active_writer_running or self.closed:
            return
        self._active_writer_running = True

        def drain() -> None:
            while True:
                with self._lock:
                    if not self._active_pending:
                        self._active_writer_running = False
                        self._active_writer_thread = None
                        return
                    account = next(iter(self._active_pending))
                    target = self._active_pending.pop(account)
                try:
                    self.set_active(account, target)
                except (OSError, ValueError):
                    pass
                finally:
                    with self._lock:
                        if (
                            account not in self._active_pending
                            and self._active_values.get(account) == target
                        ):
                            self._active_values.pop(account, None)

        thread = threading.Thread(target=drain, name="siope-active-writer", daemon=True)
        self._active_writer_thread = thread
        thread.start()

    def set_active(self, key: str, value: str | None) -> None:
        _, remote = origin(value, key)
        archive = self.archive()
        if archive is None:
            return
        if remote and archive.deleted(key, "conversation", remote):
            remote = None
        archive.put(key, "pointer", "active", {"remote": remote})

    def cache_conversation(
        self, key: str, conversation: dict[str, Any], *, make_active: bool = True
    ):
        remote = conversation.get("remote_id") or conversation.get("id")
        context, remote = origin(remote, key)
        if context != key or not remote or conversation.get("temporary"):
            return
        archive = self.archive()
        if archive is None or archive.deleted(key, "conversation", remote):
            return
        record = dict(conversation, id=identity(key, remote), remote_id=remote, context=key)
        archive.put(key, "conversation", remote, record)
        if make_active:
            self.set_active(key, remote)

    def chat_record(
        self, key: str, chat: Any, responses: list[Response] | None = None
    ) -> dict[str, Any]:
        provider_id, account_id = key.split("/", 1)
        remote_id = getattr(chat, "remote_id", None) or chat.id
        chat_project = getattr(chat, "project", None)
        project_remote = getattr(chat_project, "remote_id", None) or getattr(
            chat_project, "id", None
        )
        metadata = dict(getattr(chat, "metadata", None) or {})
        if metadata.get("project_membership_known") and not project_remote:
            self._chat_projects.pop((key, remote_id), None)
        if project_remote:
            self._chat_projects[key, remote_id] = project_remote
            if len(self._chat_projects) > 2048:
                self._chat_projects.pop(next(iter(self._chat_projects)))
        project = project_remote or self._chat_projects.get((key, remote_id))

        turn_list = responses
        if turn_list is None:
            raw_turns = getattr(getattr(chat, "responses", None), "_turns", None)
            if raw_turns:
                turn_list = list(raw_turns)
        if turn_list:
            messages = _responses_to_messages(turn_list, provider_id=provider_id)
        elif raw_messages := getattr(chat, "messages", None):
            messages = _conversation_messages_to_dicts(raw_messages)
        else:
            messages = []

        record = {
            "id": identity(key, remote_id),
            "remote_id": remote_id,
            "context": key,
            "provider_id": provider_id,
            "account_id": account_id,
            "title": chat.title,
            "project_id": identity(key, project),
            "metadata": metadata,
            **(
                {"pending_branch": metadata["pending_branch"]}
                if metadata.get("pending_branch")
                else {}
            ),
            "history_complete": not metadata.get("history_partial", False),
            "messages": messages,
        }
        return record

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
            for item in self.visible_accounts()
            if (account_keys is None or item["key"] in account_keys)
            and (selected is None or item["provider"] in selected)
            and (cursors is None or item["key"] in cursors)
        ]

        def page_for(account):
            key = account["key"]
            page_cursor = cursors.get(key) if cursors else None

            def fetch():
                page = self.open_provider(key).chats._fetch_page(
                    page_cursor, 50, include_projects=True
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

    def load(
        self, value: str, key: str, force: bool = False, *, window: bool = False, window_limit=None
    ):
        key, remote = origin(value, key)
        self.require_visible_account(key)
        archive = self.archive()
        if archive and archive.deleted(key, "conversation", remote):
            raise RemoteNotFound("Bu sohbet sağlayıcıdan silindi")

        cached = self.cached_window(key, remote, limit=window_limit) if archive else None
        pending_branch = bool(
            cached
            and isinstance(cached.get("pending_branch"), dict)
            and cached.get("branch_unanswered", True)
            and archive
            and remote
            and archive.is_pending_branch(key, remote)
        )
        if cached and pending_branch:
            return cached

        def fetch():
            try:
                prov = self.open_provider(key)
                chat, responses = prov.chats.get_with_responses(
                    remote, limit=None, include_projects=True
                )
            except RemoteNotFound:
                if archive:
                    archive.delete(key, "conversation", remote)
                raise
            self._cache_chat_handle(key, chat)
            return self._record_from_chat(key, remote, chat, responses)

        return self.read_record(
            key, "conversation", remote, fetch, force, window=window, window_limit=window_limit
        )

    def _record_from_chat(self, key: str, remote: str, chat: Any, responses: list[Response] | None):
        record = self.chat_record(key, chat, responses)
        previous = self.cached_window(key, remote) or {}
        if record.get("metadata", {}).get("title_unavailable"):
            record["title"] = previous.get("title") or record["title"]
        if not record.get("project_id"):
            record["project_id"] = previous.get("project_id")
        return record

    def delete(self, value: str, key: str, kind: str = "conversation"):
        context, remote = origin(value, key)
        if context != key or kind not in ("conversation", "container") or not remote:
            raise ValueError("Silinecek kayıt seçili hesaba ait olmalı")
        with self._lock:
            if self._sending.get(key, 0):
                raise RuntimeError("Hesabın gönderimleri tamamlandıktan sonra silin")
        prov = self.provider(key)
        capability = Capability.DELETE if kind == "conversation" else Capability.PROJECT_DELETE
        if capability not in prov.capabilities:
            raise ValueError("Sağlayıcı bu silme işlemini desteklemiyor")

        archive = self.archive()
        members = (
            archive.repository.container_members(key, remote)
            if kind == "container" and archive
            else set()
        )
        confirmed_children = None

        with self._account_gate(key):
            prov = self.open_provider(key)
            try:
                if kind == "conversation":
                    prov.chats.delete(remote)
                else:
                    prov.projects.delete(remote)
            except RemoteNotFound:
                try:
                    if kind == "conversation":
                        prov.chats.get(remote, include_projects=True, metadata_only=True)
                    else:
                        prov.projects.get(remote)
                except RemoteNotFound:
                    pass
                else:
                    raise WebAIProviderError(
                        "Sağlayıcı silmeyi doğrulamadı; uzak ve yerel kayıt korundu"
                    )
            if kind == "conversation":
                self._forget_chat_handle(key, remote)
            elif kind == "container" and members:

                def confirm(child):
                    try:
                        chat = prov.chats.get(child, include_projects=True, metadata_only=True)
                        return child if chat.metadata.get("is_visible") is False else None
                    except RemoteNotFound:
                        return child
                    except WebAIProviderError:
                        return None

                confirmed_children = set()
                children = iter(members)
                while batch := tuple(islice(children, 4)):
                    with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                        for child, res in zip(batch, pool.map(confirm, batch)):
                            if res is not None:
                                confirmed_children.add(res)

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
                    self._chat_projects.pop((key, target), None)
        except OSError as error:
            raise RuntimeError(
                "Sağlayıcıdan silindi; yerel kayıt temizlenemedi. Yenileyin."
            ) from error
        return {"removed": [identity(a, r) for a, _, r in removed], "kind": kind}

    def branch_chat(self, key: str, conversation_id: str, message_id: str):
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

        try:
            with self._account_gate(key):
                if not self.enabled(key.split("/")[0]):
                    raise ValueError("Provider is disabled")
                if archive and archive.deleted(key, "conversation", remote):
                    raise RemoteNotFound("The conversation was deleted")
                chat = self._chat_handle(key, remote)
                branched = chat.branch(message_id)
            record = self.chat_record(key, branched)
            if record["remote_id"] == remote:
                raise WebAIProviderError("The provider did not create a separate conversation")
            if not record["messages"] and source.get("messages"):
                source_messages = list(source.get("messages") or ())
                target_idx = next(
                    (
                        idx
                        for idx, msg in enumerate(source_messages)
                        if isinstance(msg, dict) and msg.get("id") == message_id
                    ),
                    None,
                )
                record["messages"] = (
                    source_messages[: target_idx + 1] if target_idx is not None else source_messages
                )
            self._cache_chat_handle(key, branched)
            if (
                record.get("metadata", {}).get("title_unavailable")
                or not str(record.get("title") or "").strip()
            ):
                record["title"] = source.get("title") or record.get("title") or "Untitled"
            if not record.get("project_id"):
                record["project_id"] = source.get("project_id")
            native_preview = isinstance(record.get("pending_branch"), dict)
            if native_preview:
                record["branch_unanswered"] = True
            self.cache_conversation(key, record, make_active=False)
            if archive and native_preview:
                archive.mark_pending_branch(key, record["remote_id"])
            return record
        finally:
            with self._lock:
                self._sending[key] -= 1

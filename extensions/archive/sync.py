"""Bounded hot cache and single-flight stale-while-revalidate coordination."""

import asyncio
import copy
import json
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import unquote

from .adapters import MAPPINGS, Mapping
from .history import merge_conversation
from .repository import encode

SYNC_MERGE_OVERLAP = 80


@dataclass(frozen=True)
class SyncChanged:
    account: str
    kind: str
    remote: str
    state: str


class Archive:
    event_type = SyncChanged

    def __init__(self, repository, files, events=None, memory_bytes=8 * 1024 * 1024):
        self.repository, self.files, self.events = repository, files, events
        pending_file_cleanup = getattr(repository, "uploaded_file_cleanup_pending", None)
        if callable(pending_file_cleanup) and pending_file_cleanup():
            files.purge_owned()
            repository.complete_uploaded_file_cleanup()
        from .downloads import DownloadHistory

        self.downloads = DownloadHistory(repository)
        self.memory_bytes = memory_bytes
        self.mappings = dict(MAPPINGS)
        self._hot, self._size = OrderedDict(), 0
        self._lock = threading.RLock()
        self._write_lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="archive-sync")
        self._pending, self._states, self._checked, self._versions = {}, OrderedDict(), {}, {}
        self._fetchers = OrderedDict()
        self.revision = 0
        self.closed = False
        self._repository_closed = False
        self._deleted = repository.tombstones() if hasattr(repository, "tombstones") else set()

    def conversations(self, account, **page):
        return self.repository.conversations(account, **page)

    def containers(self, account, **page):
        return self.repository.containers(account, **page)

    def messages(self, account, remote, **page):
        return self.repository.messages(account, remote, **page)

    def conversation_window(self, account, remote, **page):
        with self._lock:
            if self._repository_closed or (account, "conversation", remote) in self._deleted:
                return None
        page.setdefault("visible_only", True)
        return self.repository.conversation_window(account, remote, **page)

    def release(self, account, remote):
        """Release a closed view's hot body; durable data and remote work are preserved."""
        with self._lock:
            old = self._hot.pop((account, "conversation", remote), None)
            if old:
                self._size -= old[1]

    def resources(self, account, owner_kind, owner):
        return self.repository.resources(account, owner_kind, owner)

    def register_mapping(self, provider, mapping):
        self.mappings[provider] = mapping

    def _remember(self, key, value):
        old = self._hot.pop(key, None)
        if old:
            self._size -= old[1]
        # Conversation bodies are paged directly from SQL; caching a second full
        # serialized body defeats the resident-window memory bound.
        if key[1] == "conversation":
            return
        snapshot = encode(value).encode()
        size = len(snapshot)
        if size <= self.memory_bytes:
            self._hot[key] = (snapshot, size)
            self._size += size
        while self._size > self.memory_bytes:
            _, (_, removed) = self._hot.popitem(last=False)
            self._size -= removed

    def get(self, account, kind, remote):
        key = (account, kind, remote)
        with self._lock:
            if self._repository_closed or key in self._deleted:
                return None
            if key in self._hot:
                self._hot.move_to_end(key)
                snapshot = self._hot[key][0]
            else:
                snapshot = None
        if snapshot is not None:
            return json.loads(snapshot)
        # Serialize a cold read with writes, otherwise an older DB snapshot can
        # replace a newer hot value after another thread completes a local send.
        with self._write_lock:
            with self._lock:
                if self._repository_closed:
                    return None
                if key in self._hot:
                    self._hot.move_to_end(key)
                    return json.loads(self._hot[key][0])
            value = self.repository.get(*key)
            if value is not None:
                with self._lock:
                    self._remember(key, value)
            return value

    def put(self, account, kind, remote, value):
        if value.get("temporary"):
            return
        key = (account, kind, remote)
        mapping = self.mappings.get(account.split("/")[0], Mapping())
        value = mapping.normalize(kind, value)
        with self._write_lock:
            if kind == "container":
                value = mapping.merge_container(value, self.get(*key))
            with self._lock:
                if self.closed:
                    raise OSError("Archive is closed")
                if key in self._deleted:
                    return
                members = (
                    [value]
                    if kind == "conversation"
                    else value.get("items", [])
                    if kind in ("chats", "container_chats")
                    else []
                )
                for item in members:
                    project = item.get("project_id")
                    project = (
                        unquote(project.removeprefix("@" + account + "/")) if project else None
                    )
                    if project and (account, "container", project) in self._deleted:
                        item["project_id"] = None
                if kind in ("chats", "container_chats", "containers"):
                    item_kind = "container" if kind == "containers" else "conversation"
                    value["items"] = [
                        x
                        for x in value.get("items", [])
                        if (
                            account,
                            item_kind,
                            unquote(
                                (x.get("remote_id") or x.get("id") or "").removeprefix(
                                    "@" + account + "/"
                                )
                            ),
                        )
                        not in self._deleted
                    ]
            try:
                changed = self.repository.put(*key, value)
            except OSError as error:
                with self._lock:
                    self._state(key, "error", str(error))
                raise
            with self._lock:
                if self.closed:
                    return
                self._remember(key, value)
                self._written(key, changed)

    def _written(self, key, changed=True):
        self._versions[key] = self._versions.get(key, 0) + 1
        if len(self._versions) > 2048:
            for old in tuple(self._versions):
                if old not in self._pending and old != key:
                    del self._versions[old]
                    break
        if changed:
            self.revision += 1

    def save_turn(self, account, remote, value):
        """Atomically retain history hydrated while an accepted turn was saved."""
        key = (account, "conversation", remote)
        with self._write_lock:
            append = getattr(self.repository, "append_turn", None)
            if append is not None:
                mapping = self.mappings.get(account.split("/")[0], Mapping())
                value = mapping.normalize("conversation", value)
                with self._lock:
                    if self.closed:
                        raise OSError("Archive is closed")
                    if key in self._deleted:
                        return
                    project = value.get("project_id")
                    project = (
                        unquote(project.removeprefix("@" + account + "/")) if project else None
                    )
                    if project and (account, "container", project) in self._deleted:
                        value["project_id"] = None
                try:
                    append(account, remote, value)
                except OSError as error:
                    with self._lock:
                        self._state(key, "error", str(error))
                    raise
                with self._lock:
                    self._written(key)
                return
            current = self.get(account, "conversation", remote)
            merged = merge_conversation(current or {}, value)
            value.pop("_archive_base_end", None)
            self.put(account, "conversation", remote, dict(value, messages=merged["messages"]))

    def _state(self, key, state, error=None):
        self._states[key] = {
            "account": key[0],
            "kind": key[1],
            "remote": key[2],
            "state": state,
            "error": error,
            "version": self.revision + 1,
        }
        self._states.move_to_end(key)
        if len(self._states) > 256:
            for old in tuple(self._states):
                if old not in self._pending and old != key:
                    del self._states[old]
                    break
        self.revision += 1
        if self.events and not self.events.closed:
            self.events.publish(SyncChanged(*key, state))

    def _merge_partial_history(self, account, remote, value, current):
        """Merge a provider tail against a bounded SQL suffix, preserving its prefix."""
        ordinal = getattr(self.repository, "message_ordinal", None)
        replace_tail = getattr(self.repository, "replace_conversation_tail", None)
        page_messages = getattr(self.repository, "messages", None)
        messages = value.get("messages")
        if (
            not current
            or not messages
            or not callable(ordinal)
            or not callable(replace_tail)
            or not callable(page_messages)
        ):
            return False
        anchor = ordinal(account, remote, messages[0].get("id"))
        if anchor is None:
            return False
        total = (current.get("history_window") or {}).get("total", len(current.get("messages", [])))
        start = max(0, anchor - SYNC_MERGE_OVERLAP)
        local_tail = []
        offset = start
        while offset < total:
            batch = page_messages(account, remote, limit=min(500, total - offset), offset=offset)
            if not batch:
                break
            local_tail.extend(batch)
            offset += len(batch)
        local_header = dict(current)
        local_header.pop("messages", None)
        local_header.pop("history_window", None)
        merged = merge_conversation(value, dict(local_header, messages=local_tail))
        merged.pop("history_window", None)
        mapping = self.mappings.get(account.split("/")[0], Mapping())
        if not replace_tail(account, remote, start, mapping.normalize("conversation", merged)):
            return False
        key = (account, "conversation", remote)
        with self._lock:
            self._written(key)
            self._checked[key] = time.monotonic()
            if len(self._checked) > 2048:
                self._checked.pop(next(iter(self._checked)))
        return True

    def read(self, account, kind, remote, fetch, force=False, wait=True, window=False, fresh=False):
        """Serve cached records quickly, or await a bounded refresh when fresh is requested."""
        key = (account, kind, remote)
        saved = (
            (lambda: self.conversation_window(account, remote))
            if window
            else (lambda: self.get(*key))
        )
        value = saved()
        base_window = value if window and kind == "conversation" and value else None
        if kind == "conversation" and base_window is None:
            base_window = self.conversation_window(account, remote)
        base_end = (
            (base_window.get("history_window") or {}).get(
                "total", len(base_window.get("messages", []))
            )
            if isinstance(base_window, dict)
            else 0
        )
        started = False
        with self._lock:
            if self.closed or key in self._deleted:
                return value
            self._fetchers[key] = fetch
            self._fetchers.move_to_end(key)
            while len(self._fetchers) > 256:
                self._fetchers.popitem(last=False)
            future = self._pending.get(key)
            checked = self._checked.get(key)
            if future is None and (
                force or fresh or checked is None or time.monotonic() - checked > 60
            ):
                if len(self._pending) >= 32:
                    if value is not None:
                        return value
                    raise RuntimeError("Senkronizasyon kuyruğu dolu; birazdan yeniden deneyin")
                generation = self._versions.get(key, 0)
                self._state(key, "syncing")
                state_version = self._states[key]["version"]

                def work():
                    try:
                        self.repository.state(*key, "syncing")
                        result = fetch()
                        with self._write_lock:
                            with self._lock:
                                accept = (
                                    not self.closed and self._versions.get(key, 0) == generation
                                )
                            if key in self._deleted:
                                return None
                            partial_written = False
                            if (
                                kind == "conversation"
                                and "messages" in result
                                and result.get("history_complete", True) is False
                            ):
                                current = self.conversation_window(account, remote)
                                partial_written = self._merge_partial_history(
                                    account, remote, result, current
                                )
                                if partial_written:
                                    accept = True
                                elif current is not None:
                                    # Keep a lossless fallback if provider IDs no longer
                                    # overlap; this path is rare and must not drop history.
                                    current = self.get(*key)
                                    if current and "messages" in current:
                                        merged = merge_conversation(result, current)
                                        result = dict(
                                            current,
                                            messages=merged["messages"],
                                            history_complete=False,
                                        )
                                        accept = True
                            if (
                                not partial_written
                                and not accept
                                and kind == "conversation"
                                and "messages" in result
                            ):
                                current = self.conversation_window(account, remote)
                                current_total = (
                                    (current.get("history_window") or {}).get(
                                        "total", len(current.get("messages", []))
                                    )
                                    if current
                                    else 0
                                )
                                if result.get("history_complete", True) is not False and callable(
                                    getattr(self.repository, "messages", None)
                                ):
                                    # A local send can finish while provider history is in
                                    # flight. Align against only the local tail that could
                                    # have changed instead of loading a second full history.
                                    offset = max(
                                        0, min(base_end, current_total) - SYNC_MERGE_OVERLAP
                                    )
                                    local_tail = []
                                    while offset < current_total:
                                        batch = self.repository.messages(
                                            account,
                                            remote,
                                            limit=min(500, current_total - offset),
                                            offset=offset,
                                        )
                                        if not batch:
                                            break
                                        local_tail.extend(batch)
                                        offset += len(batch)
                                    merged = merge_conversation(
                                        result, dict(current or {}, messages=local_tail)
                                    )
                                    if current:
                                        header = dict(current)
                                        header.pop("messages", None)
                                        header.pop("history_window", None)
                                        result = dict(
                                            header,
                                            messages=merged["messages"],
                                            history_complete=merged.get(
                                                "history_complete",
                                                current.get("history_complete", True),
                                            ),
                                        )
                                    else:
                                        result = merged
                                else:
                                    # Partial provider pages need the local prefix so a
                                    # refresh cannot discard older archived messages.
                                    current = self.get(*key)
                                    if current and "messages" in current:
                                        merged = merge_conversation(result, current)
                                        result = dict(
                                            current,
                                            messages=merged["messages"],
                                            history_complete=merged.get(
                                                "history_complete",
                                                current.get("history_complete", True),
                                            ),
                                        )
                                accept = True
                            if accept and not partial_written:
                                self.put(*key, result)
                                with self._lock:
                                    self._checked[key] = time.monotonic()
                                    if len(self._checked) > 2048:
                                        self._checked.pop(next(iter(self._checked)))
                        self.repository.state(*key, "ready")
                        # A local send may have won while this remote read was
                        # in flight. Cold waiters must also see that newer value.
                        return saved()
                    except Exception as error:
                        with self._write_lock:
                            with self._lock:
                                if key in self._deleted:
                                    raise
                                self._checked[key] = time.monotonic()
                                if len(self._checked) > 2048:
                                    self._checked.pop(next(iter(self._checked)))
                                publish_error = (
                                    self._states.get(key, {}).get("version") == state_version
                                )
                            if publish_error:
                                self.repository.state(*key, "error", str(error))
                        raise

                future = self._pool.submit(work)
                self._pending[key] = future
                started = True

                def done(completed):
                    with self._lock:
                        self._pending.pop(key, None)
                        if not self.closed and key not in self._deleted:
                            error = (
                                completed.exception()
                                if not completed.cancelled()
                                else RuntimeError("İptal edildi")
                            )
                            if self._states.get(key, {}).get("version") == state_version:
                                self._state(
                                    key,
                                    "error" if error else "ready",
                                    str(error) if error else None,
                                )
                    self._maybe_close_repository()

        # add_done_callback may execute immediately in this caller. Register it
        # outside _lock to preserve the write-lock -> state-lock ordering.
        if started:
            future.add_done_callback(done)
        if value is not None and not fresh:
            return value
        if future is not None:
            if not wait:
                return None
            try:
                return future.result(timeout=50)
            except Exception:
                if value is not None:
                    return value
                raise
        with self._lock:
            state = self._states.get(key, {})
            if state.get("state") == "error":
                raise RuntimeError(state.get("error") or "Senkronizasyon yeniden denenecek")
        return saved()

    def refresh(self, account=None, kinds=None, exclude=()):
        """Recheck records touched in this session, without waiting for remote reads."""
        excluded = set(exclude or ())
        with self._lock:
            entries = list(self._fetchers.items())
        for key, fetch in entries:
            if key in excluded or (account is not None and key[0] != account) or (
                kinds is not None and key[1] not in kinds
            ):
                continue
            with self._lock:
                if self.closed or len(self._pending) >= 32:
                    break
            self.read(*key, fetch, force=True, wait=False)

    def states(self):
        with self._lock:
            return copy.deepcopy(list(self._states.values()))

    def mark_ready(self, account, kind, remote):
        """Clear a transient sync error after a confirmed provider operation."""
        key = (account, kind, remote)
        with self._write_lock:
            with self._lock:
                if self.closed or key in self._deleted:
                    return
            self.repository.state(*key, "ready")
            with self._lock:
                if not self.closed and key not in self._deleted:
                    self._state(key, "ready")

    def deleted(self, account, kind, remote):
        with self._lock:
            return (account, kind, remote) in self._deleted

    def delete(self, account, kind, remote, *, children=None):
        with self._write_lock:
            removed = self.repository.delete(account, kind, remote, children=children)
            with self._lock:
                self._deleted.update(removed)
                for key in removed:
                    self._versions[key] = self._versions.get(key, 0) + 1
                    self._fetchers.pop(key, None)
                    self._states.pop(key, None)
                for key in list(self._hot):
                    if key[0] == account:
                        self._size -= self._hot.pop(key)[1]
                self.revision += 1
        return removed

    def inspect_files(self):
        with self._write_lock:
            if self.closed:
                raise RuntimeError("archive is closed")
            references = self.repository.file_references()
            missing = []
            for value in references:
                path = self.files.root / value
                try:
                    relative = path.resolve().relative_to(self.files.root.resolve())
                except ValueError:
                    continue
                if len(relative.parts) == 2 and len(relative.name) == 64 and not path.is_file():
                    missing.append(value)
            return {"missing": sorted(missing), "unreferenced": self.files.collect(references)}

    def collect_files(self, dry_run=True):
        with self._write_lock:
            if self.closed:
                raise RuntimeError("archive is closed")
            return self.files.collect(self.repository.file_references(), dry_run=dry_run)

    def _maybe_close_repository(self):
        with self._write_lock:
            with self._lock:
                if not self.closed or self._pending or self._repository_closed:
                    return
                self._repository_closed = True
            self.repository.close()

    def _mark_closed(self):
        with self._lock:
            self.closed = True
            self._hot.clear()
            self._size = 0
            self._fetchers.clear()

    def close(self):
        self._mark_closed()
        self._pool.shutdown(wait=False, cancel_futures=True)
        # Fetches already running may still need repository.state(). The last
        # completion closes the backend; queued work is cancelled immediately.
        self._maybe_close_repository()

    async def aclose(self):
        self._mark_closed()
        # Async application shutdown must not leave SQLite worker threads using
        # a temporary or user data directory after the runtime has stopped.
        await asyncio.to_thread(self._pool.shutdown, wait=True, cancel_futures=True)
        self._maybe_close_repository()

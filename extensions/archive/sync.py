"""Bounded hot cache and single-flight stale-while-revalidate coordination."""

import copy
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .adapters import MAPPINGS, Mapping
from .repository import encode


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

    def conversations(self, account, **page):
        return self.repository.conversations(account, **page)

    def containers(self, account, **page):
        return self.repository.containers(account, **page)

    def messages(self, account, remote, **page):
        return self.repository.messages(account, remote, **page)

    def resources(self, account, owner_kind, owner):
        return self.repository.resources(account, owner_kind, owner)

    def register_mapping(self, provider, mapping):
        self.mappings[provider] = mapping

    def _remember(self, key, value):
        size = len(encode(value).encode())
        old = self._hot.pop(key, None)
        if old:
            self._size -= old[1]
        if size <= self.memory_bytes:
            self._hot[key] = (copy.deepcopy(value), size)
            self._size += size
        while self._size > self.memory_bytes:
            _, (_, removed) = self._hot.popitem(last=False)
            self._size -= removed

    def get(self, account, kind, remote):
        key = (account, kind, remote)
        with self._lock:
            if key in self._hot:
                self._hot.move_to_end(key)
                return copy.deepcopy(self._hot[key][0])
        value = self.repository.get(*key)
        if value is not None:
            with self._lock:
                self._remember(key, value)
        return value

    def put(self, account, kind, remote, value):
        if value.get("temporary"):
            return
        key = (account, kind, remote)
        value = self.mappings.get(account.split("/")[0], Mapping()).normalize(kind, value)
        with self._write_lock:
            with self._lock:
                if self.closed:
                    return
            try:
                changed = self.repository.put(*key, value)
            except OSError as error:
                with self._lock:
                    self._state(key, "error", str(error))
                raise
            with self._lock:
                self._remember(key, value)
                self._versions[key] = self._versions.get(key, 0) + 1
                if len(self._versions) > 2048:
                    for old in tuple(self._versions):
                        if old not in self._pending and old != key:
                            del self._versions[old]
                            break
                if changed:
                    self.revision += 1

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

    def read(self, account, kind, remote, fetch, force=False, wait=True):
        """Return a saved value immediately; one bounded worker checks the remote value."""
        key = (account, kind, remote)
        value = self.get(*key)
        with self._lock:
            if self.closed:
                return value
            self._fetchers[key] = fetch
            self._fetchers.move_to_end(key)
            while len(self._fetchers) > 256:
                self._fetchers.popitem(last=False)
            future = self._pending.get(key)
            if future is None and (force or time.monotonic() - self._checked.get(key, 0) > 60):
                if len(self._pending) >= 32:
                    if value is not None:
                        return value
                    raise RuntimeError("Senkronizasyon kuyruğu dolu; birazdan yeniden deneyin")
                generation = self._versions.get(key, 0)
                self._state(key, "syncing")

                def work():
                    self.repository.state(*key, "syncing")
                    try:
                        result = fetch()
                        with self._write_lock:
                            with self._lock:
                                accept = (
                                    not self.closed and self._versions.get(key, 0) == generation
                                )
                            if accept:
                                self.put(*key, result)
                                with self._lock:
                                    self._checked[key] = time.monotonic()
                                    if len(self._checked) > 2048:
                                        self._checked.pop(next(iter(self._checked)))
                        self.repository.state(*key, "ready")
                        return result
                    except Exception as error:
                        self.repository.state(*key, "error", str(error))
                        raise

                future = self._pool.submit(work)
                self._pending[key] = future

                def done(completed):
                    with self._lock:
                        self._pending.pop(key, None)
                        if self.closed:
                            return
                        error = (
                            completed.exception()
                            if not completed.cancelled()
                            else RuntimeError("İptal edildi")
                        )
                        self._state(
                            key, "error" if error else "ready", str(error) if error else None
                        )

                future.add_done_callback(done)
        if value is not None:
            return value
        if future is not None:
            return future.result(timeout=50) if wait else None
        return self.get(*key)

    def refresh(self):
        """Recheck records touched in this session, without waiting for remote reads."""
        with self._lock:
            entries = list(self._fetchers.items())
        for key, fetch in entries:
            self.read(*key, fetch, force=True, wait=False)

    def states(self):
        with self._lock:
            return copy.deepcopy(list(self._states.values()))

    def close(self):
        with self._lock:
            self.closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)
        self.repository.close()

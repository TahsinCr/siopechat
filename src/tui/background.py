"""Bounded terminal metadata work, independent of provider sends and archive sync."""

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor


class BackgroundReads:
    def __init__(self, workers: int = 6, capacity: int = 32) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="tui-read")
        self._slots = threading.BoundedSemaphore(capacity)
        self._lock = threading.Lock()
        self._closed = False

    def submit(self, operation: Callable[[], None]) -> bool:
        with self._lock:
            if self._closed or not self._slots.acquire(blocking=False):
                return False
            try:
                future = self._pool.submit(operation)
            except RuntimeError:
                self._slots.release()
                return False
            future.add_done_callback(lambda _: self._slots.release())
            return True

    def close(self, *, wait: bool = False) -> None:
        """Stop accepting work and optionally drain operations already running."""
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=wait, cancel_futures=True)

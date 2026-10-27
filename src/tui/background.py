"""Bounded terminal metadata work, independent of provider sends and archive sync."""

import threading
from concurrent.futures import ThreadPoolExecutor


class BackgroundReads:
    def __init__(self, workers=4, capacity=32):
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tui-read")
        self._slots = threading.BoundedSemaphore(capacity)
        self._lock = threading.Lock()
        self._closed = False

    def submit(self, operation):
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

    def close(self):
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)

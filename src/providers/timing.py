"""Opt-in, content-free ChatGPT send timing collection."""

from __future__ import annotations

import math
import os
import statistics
import threading
import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator

_TIMING_ENV = "SIOPECHAT_SEND_TIMINGS"
_current: ContextVar[SendTiming | None] = ContextVar("siopechat_send_timing", default=None)
_MAX_SAMPLES = 256
_samples: deque[SendTiming] = deque(maxlen=_MAX_SAMPLES)
_samples_lock = threading.Lock()


def timing_enabled() -> bool:
    return os.environ.get(_TIMING_ENV) == "1"


class SendTiming:
    """Keep only monotonic timestamps and named durations for one send."""

    def __init__(self) -> None:
        self.started_ns = time.perf_counter_ns()
        self._durations: dict[str, float] = {}
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def record_elapsed(self, name: str, start_ns: int) -> None:
        value = max(0, time.perf_counter_ns() - start_ns) / 1_000_000
        with self._lock:
            self._durations[name] = value

    def record(self, name: str, elapsed_ns: int) -> None:
        with self._lock:
            self._durations[name] = max(0, elapsed_ns) / 1_000_000

    def increment(self, name: str) -> None:
        with self._lock:
            self._counts[name] = self._counts.get(name, 0) + 1

    def snapshot(self) -> tuple[dict[str, float], dict[str, int]]:
        with self._lock:
            return dict(self._durations), dict(self._counts)


@contextmanager
def use_send_timing(timing: SendTiming | None) -> Iterator[None]:
    token: Token[SendTiming | None] = _current.set(timing)
    try:
        yield
    finally:
        _current.reset(token)


def current_send_timing() -> SendTiming | None:
    return _current.get()


def record_send_timing(name: str, elapsed_ns: int) -> None:
    timing = current_send_timing()
    if timing is not None:
        timing.record(name, elapsed_ns)


def collect_send_timing(timing: SendTiming | None) -> None:
    if timing is None:
        return
    with _samples_lock:
        _samples.append(timing)


def send_timing_report(*, clear: bool = True) -> str:
    with _samples_lock:
        samples = [sample.snapshot() for sample in _samples]
        if clear:
            _samples.clear()
    if not samples:
        return ""

    durations = [sample[0] for sample in samples]
    counts = [sample[1] for sample in samples]
    names = sorted({name for sample in durations for name in sample})
    rows = [f"SiopeChat ChatGPT send timings; sends={len(samples)}; milliseconds"]
    rows.append("stage | samples | median | p95")
    for name in names:
        values = sorted(sample[name] for sample in durations if name in sample)
        if not values:
            continue
        p95 = values[max(0, math.ceil(0.95 * len(values)) - 1)]
        rows.append(f"{name} | {len(values)} | {statistics.median(values):.3f} | {p95:.3f}")
    count_names = sorted({name for sample in counts for name in sample})
    if count_names:
        rows.extend(("", "path counts | total"))
        rows.extend(
            f"{name} | {sum(sample.get(name, 0) for sample in counts)}" for name in count_names
        )
    return "\n".join(rows)

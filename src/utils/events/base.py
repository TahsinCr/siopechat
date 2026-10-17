"""Common contracts and pure helpers shared by both event buses."""

from __future__ import annotations

import inspect
import math
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, Generic, TypeVar

from .contracts import ErrorPolicy, EventBusStats
from .errors import EventBusClosedError
from .filtering import EventFilter, EventType, coerce_event_filter
from .history import EventHistory, MemoryEventHistory


T = TypeVar("T")


def validate_positive_capacity(value: int, *, name: str) -> int:
    """Validate a bounded collection/schedule capacity."""

    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def normalize_replay_capacity(value: int) -> int:
    """Preserve the event buses' established replay-capacity normalization."""

    if value <= 0:
        raise ValueError("Replay buffer limit must be positive")
    return int(value)


def validate_positive_timeout(value: float, *, name: str) -> float:
    """Validate and normalize a finite timeout value."""

    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be positive and finite")
    return float(value)


class BaseEventBus(Generic[T], ABC):
    def __init__(
        self,
        *,
        history: EventHistory[T] | int | None = None,
        error_policy: ErrorPolicy = ErrorPolicy.ISOLATE,
    ) -> None:
        if isinstance(history, bool):
            raise TypeError("Event history must be a capacity or EventHistory")
        self._history = MemoryEventHistory[T](history) if isinstance(history, int) else history
        self._error_policy = ErrorPolicy(error_policy)
        self._published = 0
        self._delivered = 0
        self._failed = 0

    @property
    def history(self) -> EventHistory[T] | None:
        return self._history

    @property
    def error_policy(self) -> ErrorPolicy:
        return self._error_policy

    @property
    @abstractmethod
    def closed(self) -> bool: ...

    @property
    @abstractmethod
    def subscriber_count(self) -> int: ...

    @property
    @abstractmethod
    def stats(self) -> EventBusStats: ...

    def _ensure_open(self) -> None:
        if self.closed:
            raise EventBusClosedError("Event bus is closed")

    def _record(self, event: T) -> None:
        if self._history is not None:
            self._history.append(event)

    @staticmethod
    def _delivery_limit(*, once: bool, times: int | None) -> int | None:
        if times is not None:
            if isinstance(times, bool) or not isinstance(times, int) or times <= 0:
                raise ValueError("Subscription times must be a positive integer")
            if once and times != 1:
                raise ValueError("Use once=True or times, not both")
            return times
        return 1 if once else None

    def latest(
        self,
        *,
        event_filter: EventFilter[T] | None = None,
        event_type: EventType | None = None,
        predicate: Callable[[T], bool] | None = None,
    ) -> T | None:
        if self._history is None:
            return None
        normalized_filter = coerce_event_filter(
            event_filter,
            event_type=event_type,
            predicate=predicate,
        )
        return self._history.latest(normalized_filter)

    def query(
        self,
        *,
        event_filter: EventFilter[T] | None = None,
        event_type: EventType | None = None,
        predicate: Callable[[T], bool] | None = None,
        limit: int | None = None,
    ) -> tuple[T, ...]:
        if self._history is None:
            return ()
        normalized_filter = coerce_event_filter(
            event_filter,
            event_type=event_type,
            predicate=predicate,
        )
        return self._history.query(normalized_filter, limit=limit)

    @staticmethod
    def _is_async_handler(handler: Any) -> bool:
        if inspect.iscoroutinefunction(handler):
            return True
        call = getattr(handler, "__call__", None)
        return call is not None and inspect.iscoroutinefunction(call)

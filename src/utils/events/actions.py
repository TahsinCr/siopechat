"""Optional hooks shared by every operation on an event bus."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from .contracts import PublishResult
from .filtering import EventFilter


T = TypeVar("T")


@dataclass(frozen=True)
class EventErrorContext(Generic[T]):
    event: T
    error: Exception


@dataclass(frozen=True)
class EventTimeoutContext(Generic[T]):
    event_filter: EventFilter[T]
    timeout: float | None


@dataclass(frozen=True)
class EventBusActions(Generic[T]):
    on_error: Callable[[EventErrorContext[T]], None] | None = None
    on_timeout: Callable[[EventTimeoutContext[T]], None] | None = None
    on_before: Callable[[T], None] | None = None
    on_after: Callable[[T, PublishResult], None] | None = None

    def __post_init__(self) -> None:
        for name in ("on_before", "on_after", "on_error", "on_timeout"):
            action = getattr(self, name)
            if action is not None and not callable(action):
                raise TypeError(f"Event action {name} must be callable")


@dataclass(frozen=True)
class AsyncEventBusActions(Generic[T]):
    on_error: Callable[[EventErrorContext[T]], Awaitable[None]] | None = None
    on_timeout: Callable[[EventTimeoutContext[T]], Awaitable[None]] | None = None
    on_before: Callable[[T], Awaitable[None]] | None = None
    on_after: Callable[[T, PublishResult], Awaitable[None]] | None = None

    def __post_init__(self) -> None:
        for name in ("on_before", "on_after", "on_error", "on_timeout"):
            action = getattr(self, name)
            if action is not None and not callable(action):
                raise TypeError(f"Async event action {name} must be callable")

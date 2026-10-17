"""Application-level engines for managing named event channels."""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import Executor
from typing import Any

from .actions import (
    AsyncEventBusActions,
    EventBusActions,
    EventErrorContext,
    EventTimeoutContext,
)
from .async_event_bus import AsyncEventBus
from .contracts import DeliveryMode, ErrorPolicy, PublishResult
from .event_bus import EventBus
from .filtering import EventFilter, EventType
from .subscription import AsyncSubscription, Subscription


def _channel_name(name: str) -> str:
    normalized = str(name).strip().lower()
    if not normalized:
        raise ValueError("Event channel name cannot be empty")
    return normalized


class EventEngine:
    """Create and manage thread-safe event buses by a readable channel name."""

    def __init__(
        self,
        *,
        history: int | None = None,
        error_policy: ErrorPolicy = ErrorPolicy.ISOLATE,
        executor: Executor | None = None,
        actions: EventBusActions[Any] | None = None,
        on_before: Callable[[Any], None] | None = None,
        on_after: Callable[[Any, PublishResult], None] | None = None,
        on_error: Callable[[EventErrorContext[Any]], None] | None = None,
        on_timeout: Callable[[EventTimeoutContext[Any]], None] | None = None,
    ) -> None:
        self._history = history
        self._error_policy = ErrorPolicy(error_policy)
        self._executor = executor
        direct_actions = (on_before, on_after, on_error, on_timeout)
        if actions is not None and any(action is not None for action in direct_actions):
            raise ValueError("Use actions or direct on_* callbacks, not both")
        self._actions = actions or EventBusActions(
            on_before=on_before,
            on_after=on_after,
            on_error=on_error,
            on_timeout=on_timeout,
        )
        self._channels: dict[str, EventBus[Any]] = {}
        self._lock = threading.RLock()
        self._running = True

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def channel_names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._channels))

    def channel(self, name: str) -> EventBus[Any]:
        """Return a channel, creating it with engine defaults when necessary."""

        name = _channel_name(name)
        with self._lock:
            bus = self._channels.get(name)
            if bus is None:
                bus = EventBus(
                    history=self._history,
                    error_policy=self._error_policy,
                    executor=self._executor,
                    actions=self._actions,
                )
                self._channels[name] = bus
            self._running = True
            return bus

    def add(self, name: str, bus: EventBus[Any]) -> EventBus[Any]:
        """Register a customized bus under a channel name."""

        if not isinstance(bus, EventBus):
            raise TypeError("Event engine channels must be EventBus instances")
        name = _channel_name(name)
        with self._lock:
            if name in self._channels:
                raise ValueError(f"Event channel already exists: {name}")
            self._channels[name] = bus
            self._running = True
        return bus

    def publish(self, channel: str, event: Any) -> PublishResult:
        return self.channel(channel).publish(event)

    def subscribe(
        self,
        channel: str,
        callback: Callable[[Any], None],
        **options: Any,
    ) -> Subscription:
        return self.channel(channel).subscribe(callback, **options)

    def once(
        self,
        channel: str,
        callback: Callable[[Any], None],
        **options: Any,
    ) -> Subscription:
        return self.channel(channel).once(callback, **options)

    def publish_every(
        self,
        channel: str,
        event: Any,
        interval: float,
        *,
        times: int | None = None,
        immediately: bool = True,
    ) -> Subscription:
        return self.channel(channel).publish_every(
            event,
            interval,
            times=times,
            immediately=immediately,
        )

    def wait_for(
        self,
        channel: str,
        *,
        event_filter: EventFilter[Any] | None = None,
        event_type: EventType | None = None,
        predicate: Callable[[Any], bool] | None = None,
        timeout: float | None = None,
    ) -> Any | None:
        return self.channel(channel).wait_for(
            event_filter=event_filter,
            event_type=event_type,
            predicate=predicate,
            timeout=timeout,
        )

    def remove(self, name: str) -> bool:
        name = _channel_name(name)
        with self._lock:
            bus = self._channels.pop(name, None)
        if bus is None:
            return False
        bus.close()
        return True

    def start(self) -> None:
        """Keep Service-style lifecycle symmetry; channels start lazily."""

        with self._lock:
            self._running = True

    def stop(self) -> None:
        with self._lock:
            channels = tuple(self._channels.values())
            self._channels.clear()
            self._running = False
        for channel in channels:
            channel.close()

    close = stop

    def __enter__(self) -> "EventEngine":
        return self

    def __exit__(self, *_args: object) -> None:
        self.stop()


class AsyncEventEngine:
    """Create and manage asyncio-native event buses by channel name."""

    def __init__(
        self,
        *,
        history: int | None = None,
        error_policy: ErrorPolicy = ErrorPolicy.ISOLATE,
        delivery_mode: DeliveryMode = DeliveryMode.SEQUENTIAL,
        actions: AsyncEventBusActions[Any] | None = None,
        on_before: Callable[[Any], Awaitable[None]] | None = None,
        on_after: Callable[[Any, PublishResult], Awaitable[None]] | None = None,
        on_error: Callable[[EventErrorContext[Any]], Awaitable[None]] | None = None,
        on_timeout: Callable[[EventTimeoutContext[Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._history = history
        self._error_policy = ErrorPolicy(error_policy)
        self._delivery_mode = DeliveryMode(delivery_mode)
        direct_actions = (on_before, on_after, on_error, on_timeout)
        if actions is not None and any(action is not None for action in direct_actions):
            raise ValueError("Use actions or direct on_* callbacks, not both")
        self._actions = actions or AsyncEventBusActions(
            on_before=on_before,
            on_after=on_after,
            on_error=on_error,
            on_timeout=on_timeout,
        )
        self._channels: dict[str, AsyncEventBus[Any]] = {}
        self._lock = threading.RLock()
        self._running = True

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def channel_names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._channels))

    def channel(self, name: str) -> AsyncEventBus[Any]:
        name = _channel_name(name)
        with self._lock:
            bus = self._channels.get(name)
            if bus is None:
                bus = AsyncEventBus(
                    history=self._history,
                    error_policy=self._error_policy,
                    delivery_mode=self._delivery_mode,
                    actions=self._actions,
                )
                self._channels[name] = bus
            self._running = True
            return bus

    def add(self, name: str, bus: AsyncEventBus[Any]) -> AsyncEventBus[Any]:
        if not isinstance(bus, AsyncEventBus):
            raise TypeError("Async event engine channels must be AsyncEventBus instances")
        name = _channel_name(name)
        with self._lock:
            if name in self._channels:
                raise ValueError(f"Async event channel already exists: {name}")
            self._channels[name] = bus
            self._running = True
        return bus

    async def publish(self, channel: str, event: Any) -> PublishResult:
        return await self.channel(channel).publish(event)

    async def subscribe(
        self,
        channel: str,
        callback: Callable[[Any], Awaitable[None]],
        **options: Any,
    ) -> AsyncSubscription:
        return await self.channel(channel).subscribe(callback, **options)

    async def once(
        self,
        channel: str,
        callback: Callable[[Any], Awaitable[None]],
        **options: Any,
    ) -> AsyncSubscription:
        return await self.channel(channel).once(callback, **options)

    async def publish_every(
        self,
        channel: str,
        event: Any,
        interval: float,
        *,
        times: int | None = None,
        immediately: bool = True,
    ) -> AsyncSubscription:
        return await self.channel(channel).publish_every(
            event,
            interval,
            times=times,
            immediately=immediately,
        )

    async def wait_for(
        self,
        channel: str,
        *,
        event_filter: EventFilter[Any] | None = None,
        event_type: EventType | None = None,
        predicate: Callable[[Any], bool] | None = None,
        timeout: float | None = None,
    ) -> Any | None:
        return await self.channel(channel).wait_for(
            event_filter=event_filter,
            event_type=event_type,
            predicate=predicate,
            timeout=timeout,
        )

    async def remove(self, name: str) -> bool:
        name = _channel_name(name)
        with self._lock:
            bus = self._channels.pop(name, None)
        if bus is None:
            return False
        await bus.close()
        return True

    async def start(self) -> None:
        """Keep lifecycle symmetry; channels bind to a loop when first used."""

        with self._lock:
            self._running = True

    async def stop(self) -> None:
        with self._lock:
            channels = tuple(self._channels.values())
            self._channels.clear()
            self._running = False
        for channel in channels:
            await channel.close()

    close = stop

    async def __aenter__(self) -> "AsyncEventEngine":
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.stop()

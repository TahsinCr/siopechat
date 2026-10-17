"""Asyncio-native event bus."""

from __future__ import annotations

import asyncio
import math
from collections import deque
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from dataclasses import dataclass, field as dataclass_field
from typing import Generic, TypeVar

from ..compatibility import ExceptionGroup

from .actions import AsyncEventBusActions, EventErrorContext, EventTimeoutContext
from .base import BaseEventBus, normalize_replay_capacity, validate_positive_capacity
from .contracts import DeliveryMode, ErrorPolicy, EventBusStats, PublishResult
from .errors import EventBusError, InvalidEventHandlerError
from .filtering import EventFilter, EventType, coerce_event_filter
from .history import EventHistory
from .subscription import AsyncSubscription


T = TypeVar("T")


@dataclass(slots=True)
class _AsyncSubscriber(Generic[T]):
    callback: Callable[[T], Awaitable[None]]
    event_filter: EventFilter[T]
    remaining: int | None
    subscription: AsyncSubscription
    replaying: bool = False
    pending: deque[T] = dataclass_field(default_factory=deque)

    def claim(self) -> tuple[bool, bool]:
        """Reserve one delivery and report whether it is the final one."""

        if not self.subscription.active:
            return False, False
        if self.remaining is None:
            return True, False
        if self.remaining <= 0:
            return False, False
        self.remaining -= 1
        return True, self.remaining == 0

    def buffer_if_replaying(self, event: T) -> bool:
        if not self.replaying:
            return False
        if self.pending.maxlen is not None and len(self.pending) >= self.pending.maxlen:
            raise BufferError("Event replay buffer is full")
        self.pending.append(event)
        return True

    def finish_replay(self) -> tuple[T, ...] | None:
        if self.pending:
            events = tuple(self.pending)
            self.pending.clear()
            return events
        self.replaying = False
        return None


class AsyncEventBus(BaseEventBus[T]):
    """Publish events to async handlers on one owning event loop."""

    def __init__(
        self,
        *,
        history: EventHistory[T] | int | None = None,
        error_policy: ErrorPolicy = ErrorPolicy.ISOLATE,
        delivery_mode: DeliveryMode = DeliveryMode.SEQUENTIAL,
        replay_buffer_limit: int = 1_000,
        max_schedules: int = 64,
        actions: AsyncEventBusActions[T] | None = None,
        on_before: Callable[[T], Awaitable[None]] | None = None,
        on_after: Callable[[T, PublishResult], Awaitable[None]] | None = None,
        on_error: Callable[[EventErrorContext[T]], Awaitable[None]] | None = None,
        on_timeout: Callable[[EventTimeoutContext[T]], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(history=history, error_policy=error_policy)
        replay_buffer_limit = normalize_replay_capacity(replay_buffer_limit)
        max_schedules = validate_positive_capacity(
            max_schedules,
            name="Maximum periodic schedules",
        )
        direct_actions = (on_before, on_after, on_error, on_timeout)
        if actions is not None and any(action is not None for action in direct_actions):
            raise ValueError("Use actions or direct on_* callbacks, not both")
        self._actions = actions or AsyncEventBusActions(
            on_before=on_before,
            on_after=on_after,
            on_error=on_error,
            on_timeout=on_timeout,
        )
        for action in (
            self._actions.on_before,
            self._actions.on_after,
            self._actions.on_error,
            self._actions.on_timeout,
        ):
            if action is not None and not self._is_async_handler(action):
                raise InvalidEventHandlerError(
                    "AsyncEventBus actions must be async"
                )
        self._delivery_mode = DeliveryMode(delivery_mode)
        self._replay_buffer_limit = replay_buffer_limit
        self._max_schedules = max_schedules
        self._subscribers: dict[int, _AsyncSubscriber[T]] = {}
        self._next_id = 0
        self._closed = False
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._waiters: set[asyncio.Future[T | None]] = set()
        self._schedules: dict[int, tuple[AsyncSubscription, asyncio.Task[None]]] = {}

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def stats(self) -> EventBusStats:
        return EventBusStats(self._published, self._delivered, self._failed)

    @property
    def delivery_mode(self) -> DeliveryMode:
        return self._delivery_mode

    def _bind_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise EventBusError("AsyncEventBus cannot be shared across event loops")
        return loop

    async def subscribe(
        self,
        callback: Callable[[T], Awaitable[None]],
        *,
        event_filter: EventFilter[T] | None = None,
        event_type: EventType | None = None,
        predicate: Callable[[T], bool] | None = None,
        once: bool = False,
        times: int | None = None,
        replay: int = 0,
    ) -> AsyncSubscription:
        self._bind_loop()
        if not callable(callback) or not self._is_async_handler(callback):
            raise InvalidEventHandlerError(
                "AsyncEventBus accepts async handlers only"
            )
        if replay < 0:
            raise ValueError("Replay count cannot be negative")
        delivery_limit = self._delivery_limit(once=once, times=times)
        normalized_filter = coerce_event_filter(
            event_filter,
            event_type=event_type,
            predicate=predicate,
        )
        async with self._lock:
            self._ensure_open()
            history = self._history
            subscription, subscriber = self._subscribe_locked(
                callback,
                normalized_filter,
                delivery_limit,
                replaying=bool(replay and history is not None),
            )
            replay_snapshot = (
                history.query()
                if subscriber.replaying and history is not None
                else ()
            )

        replay_events = tuple(
            event for event in replay_snapshot if normalized_filter.matches(event)
        )[-replay:]

        if subscriber.replaying:
            try:
                pending: tuple[T, ...] | None = replay_events
                while pending is not None:
                    for event in pending:
                        claimed, final = subscriber.claim()
                        if not claimed:
                            break
                        if final:
                            await subscription._consume()
                        try:
                            await callback(event)
                        except Exception as exc:
                            action_errors = await self._notify_errors(event, (exc,))
                            self._failed += 1 + len(action_errors)
                            if self._error_policy is ErrorPolicy.RAISE:
                                await subscription.cancel()
                                raise ExceptionGroup(
                                    "Async event replay failed",
                                    [exc, *action_errors],
                                ) from exc
                        else:
                            self._delivered += 1
                            if final:
                                break
                    pending = subscriber.finish_replay()
            except BaseException:
                await asyncio.shield(subscription.cancel())
                raise
        return subscription

    def _subscribe_locked(
        self,
        callback: Callable[[T], Awaitable[None]],
        event_filter: EventFilter[T],
        delivery_limit: int | None,
        replaying: bool = False,
    ) -> tuple[AsyncSubscription, _AsyncSubscriber[T]]:
        subscription_id = self._next_id
        self._next_id += 1

        async def cancel() -> None:
            async with self._lock:
                self._subscribers.pop(subscription_id, None)

        subscription = AsyncSubscription(subscription_id, cancel)
        subscriber = _AsyncSubscriber(
            callback,
            event_filter,
            delivery_limit,
            subscription,
            replaying=replaying,
            pending=deque(maxlen=self._replay_buffer_limit),
        )
        self._subscribers[subscription_id] = subscriber
        return subscription, subscriber

    async def once(
        self,
        callback: Callable[[T], Awaitable[None]],
        *,
        event_filter: EventFilter[T] | None = None,
        event_type: EventType | None = None,
        predicate: Callable[[T], bool] | None = None,
        replay: int = 0,
    ) -> AsyncSubscription:
        """Subscribe a handler for its first matching event only."""

        return await self.subscribe(
            callback,
            event_filter=event_filter,
            event_type=event_type,
            predicate=predicate,
            once=True,
            replay=replay,
        )

    async def publish(self, event: T) -> PublishResult:
        self._bind_loop()
        async with self._lock:
            self._ensure_open()
            subscribers = tuple(self._subscribers.values())
            self._published += 1
            self._record(event)

        matched: list[_AsyncSubscriber[T]] = []
        errors: list[Exception] = []
        if self._actions.on_before is not None:
            try:
                await self._actions.on_before(event)
            except Exception as exc:
                errors.append(exc)
        for subscriber in subscribers:
            if not subscriber.subscription.active:
                continue
            try:
                matches = subscriber.event_filter.matches(event)
            except Exception as exc:
                errors.append(exc)
                continue
            if matches:
                try:
                    if subscriber.buffer_if_replaying(event):
                        continue
                except BufferError as exc:
                    await subscriber.subscription.cancel()
                    errors.append(exc)
                    continue
                matched.append(subscriber)

        delivered = 0
        if self._delivery_mode is DeliveryMode.SEQUENTIAL:
            for subscriber in matched:
                was_delivered, error = await self._deliver(subscriber, event)
                if was_delivered:
                    delivered += 1
                if error is not None:
                    errors.append(error)
        else:
            outcomes = await asyncio.gather(
                *(self._deliver(subscriber, event) for subscriber in matched),
            )
            for was_delivered, error in outcomes:
                if was_delivered:
                    delivered += 1
                if error is not None:
                    errors.append(error)

        errors.extend(await self._notify_errors(event, tuple(errors)))
        result = PublishResult(
            matched=len(matched),
            delivered=delivered,
            failed=len(errors),
            errors=tuple(errors),
        )
        if self._actions.on_after is not None:
            try:
                await self._actions.on_after(event, result)
            except Exception as exc:
                errors.append(exc)
                errors.extend(await self._notify_errors(event, (exc,)))
                result = PublishResult(
                    matched=len(matched),
                    delivered=delivered,
                    failed=len(errors),
                    errors=tuple(errors),
                )
        self._delivered += delivered
        self._failed += len(errors)
        if errors and self._error_policy is ErrorPolicy.RAISE:
            raise ExceptionGroup("Async event delivery failed", errors)
        return result

    async def _notify_errors(
        self,
        event: T,
        errors: tuple[Exception, ...],
    ) -> list[Exception]:
        action = self._actions.on_error
        if action is None:
            return []
        action_errors: list[Exception] = []
        for error in errors:
            try:
                await action(EventErrorContext(event, error))
            except Exception as exc:
                action_errors.append(exc)
        return action_errors

    async def _deliver(
        self,
        subscriber: _AsyncSubscriber[T],
        event: T,
    ) -> tuple[bool, Exception | None]:
        claimed, final = subscriber.claim()
        if not claimed:
            return False, None
        if final:
            await subscriber.subscription._consume()
        try:
            await subscriber.callback(event)
        except Exception as exc:
            return False, exc
        return True, None

    async def publish_every(
        self,
        event: T,
        interval: float,
        *,
        times: int | None = None,
        immediately: bool = True,
    ) -> AsyncSubscription:
        """Publish the same event periodically until cancelled or completed."""

        self._bind_loop()
        interval = float(interval)
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("Event publish interval must be positive and finite")
        if times is not None and (
            isinstance(times, bool) or not isinstance(times, int) or times <= 0
        ):
            raise ValueError("Periodic publish times must be a positive integer")

        async with self._lock:
            self._ensure_open()
            if len(self._schedules) >= self._max_schedules:
                raise EventBusError("Maximum periodic schedule capacity reached")
            schedule_id = self._next_id
            self._next_id += 1
            task: asyncio.Task[None] | None = None

            async def cancel() -> None:
                if task is not None and task is not asyncio.current_task():
                    task.cancel()
                async with self._lock:
                    self._schedules.pop(schedule_id, None)

            subscription = AsyncSubscription(schedule_id, cancel)

            async def run() -> None:
                completed = 0
                try:
                    if not immediately:
                        await asyncio.sleep(interval)
                    while subscription.active and not self.closed:
                        try:
                            await self.publish(event)
                        except Exception:
                            if self._error_policy is ErrorPolicy.RAISE:
                                break
                        completed += 1
                        if times is not None and completed >= times:
                            break
                        if not subscription.active:
                            break
                        await asyncio.sleep(interval)
                finally:
                    await subscription._deactivate()
                    async with self._lock:
                        self._schedules.pop(schedule_id, None)

            task = asyncio.create_task(run(), name=f"AsyncEventBusPeriodic-{schedule_id}")
            self._schedules[schedule_id] = (subscription, task)
        return subscription

    async def wait_for(
        self,
        *,
        event_filter: EventFilter[T] | None = None,
        event_type: EventType | None = None,
        predicate: Callable[[T], bool] | None = None,
        timeout: float | None = None,
    ) -> T | None:
        loop = self._bind_loop()
        if timeout is not None and timeout < 0:
            raise ValueError("Event wait timeout cannot be negative")
        normalized_filter = coerce_event_filter(
            event_filter,
            event_type=event_type,
            predicate=predicate,
        )
        result: asyncio.Future[T | None] = loop.create_future()

        async def capture(event: T) -> None:
            if not result.done():
                result.set_result(event)

        async with self._lock:
            self._ensure_open()
            subscription, _subscriber = self._subscribe_locked(
                capture,
                normalized_filter,
                1,
            )
            self._waiters.add(result)

        timed_out = False
        try:
            value = await asyncio.wait_for(result, timeout)
        except asyncio.TimeoutError:
            timed_out = True
            value = None
        finally:
            await subscription.cancel()
            async with self._lock:
                self._waiters.discard(result)
        if timed_out and self._actions.on_timeout is not None:
            await self._actions.on_timeout(
                EventTimeoutContext(normalized_filter, timeout)
            )
        return value

    def publish_threadsafe(self, event: T) -> Future[PublishResult]:
        loop = self._loop
        if loop is None or loop.is_closed() or not loop.is_running():
            raise EventBusError("AsyncEventBus has no active owning event loop")
        operation = self.publish(event)
        try:
            return asyncio.run_coroutine_threadsafe(operation, loop)
        except RuntimeError as exc:
            operation.close()
            raise EventBusError("AsyncEventBus owning event loop is unavailable") from exc

    async def clear(self) -> None:
        self._bind_loop()
        async with self._lock:
            subscriptions = tuple(
                subscriber.subscription for subscriber in self._subscribers.values()
            )
            self._subscribers.clear()
            waiters = tuple(self._waiters)
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_result(None)
        for subscription in subscriptions:
            await subscription._deactivate()

    async def close(self) -> None:
        self._bind_loop()
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            subscriptions = tuple(
                subscriber.subscription for subscriber in self._subscribers.values()
            )
            schedules = tuple(self._schedules.values())
            self._subscribers.clear()
            self._schedules.clear()
            waiters = tuple(self._waiters)
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_result(None)
        for subscription in subscriptions:
            await subscription._deactivate()
        for schedule, _task in schedules:
            await schedule.cancel()
        current_task = asyncio.current_task()
        pending_tasks = tuple(
            task for _schedule, task in schedules if task is not current_task
        )
        if pending_tasks:
            await asyncio.gather(
                *pending_tasks,
                return_exceptions=True,
            )

    async def __aenter__(self) -> "AsyncEventBus[T]":
        self._bind_loop()
        self._ensure_open()
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

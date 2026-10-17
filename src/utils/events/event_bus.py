"""Thread-safe synchronous event bus."""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Executor, Future
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from ..compatibility import ExceptionGroup

from .actions import EventBusActions, EventErrorContext, EventTimeoutContext
from .base import (
    BaseEventBus,
    normalize_replay_capacity,
    validate_positive_capacity,
    validate_positive_timeout,
)
from .contracts import ErrorPolicy, EventBusStats, PublishResult
from .errors import EventBusError, EventShutdownTimeoutError, InvalidEventHandlerError
from .filtering import EventFilter, EventType, coerce_event_filter
from .history import EventHistory
from .subscription import Subscription


T = TypeVar("T")


@dataclass(slots=True)
class _Subscriber(Generic[T]):
    callback: Callable[[T], None]
    event_filter: EventFilter[T]
    remaining: int | None
    subscription: Subscription
    lock: threading.Lock = field(default_factory=threading.Lock)
    replaying: bool = False
    pending: deque[T] = field(default_factory=deque)

    def claim(self) -> tuple[bool, bool]:
        """Reserve one delivery and report whether it is the final one."""

        with self.lock:
            if not self.subscription.active:
                return False, False
            if self.remaining is None:
                return True, False
            if self.remaining <= 0:
                return False, False
            self.remaining -= 1
            return True, self.remaining == 0

    def buffer_if_replaying(self, event: T) -> bool:
        with self.lock:
            if not self.replaying:
                return False
            if self.pending.maxlen is not None and len(self.pending) >= self.pending.maxlen:
                raise BufferError("Event replay buffer is full")
            self.pending.append(event)
            return True

    def finish_replay(self) -> tuple[T, ...] | None:
        with self.lock:
            if self.pending:
                events = tuple(self.pending)
                self.pending.clear()
                return events
            self.replaying = False
            return None


class EventBus(BaseEventBus[T]):
    """Publish events safely from any thread to synchronous subscribers."""

    def __init__(
        self,
        *,
        history: EventHistory[T] | int | None = None,
        error_policy: ErrorPolicy = ErrorPolicy.ISOLATE,
        executor: Executor | None = None,
        replay_buffer_limit: int = 1_000,
        max_schedules: int = 64,
        shutdown_timeout: float = 5.0,
        actions: EventBusActions[T] | None = None,
        on_before: Callable[[T], None] | None = None,
        on_after: Callable[[T, PublishResult], None] | None = None,
        on_error: Callable[[EventErrorContext[T]], None] | None = None,
        on_timeout: Callable[[EventTimeoutContext[T]], None] | None = None,
    ) -> None:
        super().__init__(history=history, error_policy=error_policy)
        replay_buffer_limit = normalize_replay_capacity(replay_buffer_limit)
        max_schedules = validate_positive_capacity(
            max_schedules,
            name="Maximum periodic schedules",
        )
        shutdown_timeout = validate_positive_timeout(
            shutdown_timeout,
            name="Shutdown timeout",
        )
        direct_actions = (on_before, on_after, on_error, on_timeout)
        if actions is not None and any(action is not None for action in direct_actions):
            raise ValueError("Use actions or direct on_* callbacks, not both")
        self._actions = actions or EventBusActions(
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
            if action is not None and self._is_async_handler(action):
                raise InvalidEventHandlerError(
                    "EventBus actions must be synchronous"
                )
        self._executor = executor
        self._executor_context = threading.local()
        self._replay_buffer_limit = replay_buffer_limit
        self._max_schedules = max_schedules
        self._shutdown_timeout = shutdown_timeout
        self._subscribers: dict[int, _Subscriber[T]] = {}
        self._next_id = 0
        self._closed = False
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._schedules: dict[int, tuple[Subscription, threading.Thread]] = {}

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    @property
    def stats(self) -> EventBusStats:
        with self._lock:
            return EventBusStats(self._published, self._delivered, self._failed)

    def subscribe(
        self,
        callback: Callable[[T], None],
        *,
        event_filter: EventFilter[T] | None = None,
        event_type: EventType | None = None,
        predicate: Callable[[T], bool] | None = None,
        once: bool = False,
        times: int | None = None,
        replay: int = 0,
    ) -> Subscription:
        if not callable(callback):
            raise InvalidEventHandlerError("Event handler must be callable")
        if self._is_async_handler(callback):
            raise InvalidEventHandlerError(
                "EventBus accepts synchronous handlers; use AsyncEventBus"
            )
        if replay < 0:
            raise ValueError("Replay count cannot be negative")
        delivery_limit = self._delivery_limit(once=once, times=times)
        normalized_filter = coerce_event_filter(
            event_filter,
            event_type=event_type,
            predicate=predicate,
        )
        with self._lock:
            self._ensure_open()
            history = self._history
            subscription_id = self._next_id
            self._next_id += 1

            def cancel() -> None:
                with self._lock:
                    self._subscribers.pop(subscription_id, None)

            subscription = Subscription(subscription_id, cancel)
            subscriber = _Subscriber(
                callback,
                normalized_filter,
                delivery_limit,
                subscription,
                replaying=bool(replay and history is not None),
                pending=deque(maxlen=self._replay_buffer_limit),
            )
            self._subscribers[subscription_id] = subscriber
            replay_snapshot = (
                history.query()
                if subscriber.replaying and history is not None
                else ()
            )

        replay_events = tuple(
            event for event in replay_snapshot if normalized_filter.matches(event)
        )[-replay:]

        if subscriber.replaying:
            pending: tuple[T, ...] | None = replay_events
            while pending is not None:
                for event in pending:
                    claimed, final = subscriber.claim()
                    if not claimed:
                        break
                    if final:
                        subscription._consume()
                    try:
                        callback(event)
                    except Exception as exc:
                        action_errors = self._notify_errors(event, (exc,))
                        with self._lock:
                            self._failed += 1 + len(action_errors)
                        if self._error_policy is ErrorPolicy.RAISE:
                            subscription.cancel()
                            raise ExceptionGroup(
                                "Event replay failed",
                                [exc, *action_errors],
                            ) from exc
                    else:
                        with self._lock:
                            self._delivered += 1
                        if final:
                            break
                pending = subscriber.finish_replay()
        return subscription

    def once(
        self,
        callback: Callable[[T], None],
        *,
        event_filter: EventFilter[T] | None = None,
        event_type: EventType | None = None,
        predicate: Callable[[T], bool] | None = None,
        replay: int = 0,
    ) -> Subscription:
        """Subscribe a handler for its first matching event only."""

        return self.subscribe(
            callback,
            event_filter=event_filter,
            event_type=event_type,
            predicate=predicate,
            once=True,
            replay=replay,
        )

    def publish(self, event: T) -> PublishResult:
        with self._lock:
            self._ensure_open()
            subscribers = tuple(self._subscribers.values())
            self._published += 1
            self._record(event)

        matched: list[_Subscriber[T]] = []
        errors: list[Exception] = []
        if self._actions.on_before is not None:
            try:
                self._actions.on_before(event)
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
                    subscriber.subscription.cancel()
                    errors.append(exc)
                    continue
                matched.append(subscriber)

        delivered = 0
        if self._executor is None or getattr(self._executor_context, "active", False):
            for subscriber in matched:
                was_delivered, error = self._deliver(subscriber, event)
                if was_delivered:
                    delivered += 1
                if error is not None:
                    errors.append(error)
        else:
            futures: list[tuple[_Subscriber[T], Future[None]]] = []
            for subscriber in matched:
                claimed, final = subscriber.claim()
                if not claimed:
                    continue
                if final:
                    subscriber.subscription._consume()
                try:
                    future = self._executor.submit(
                        self._invoke_executor_callback,
                        subscriber.callback,
                        event,
                    )
                except Exception as exc:
                    errors.append(exc)
                else:
                    futures.append((subscriber, future))
            for _subscriber, future in futures:
                try:
                    future.result()
                except Exception as exc:
                    errors.append(exc)
                else:
                    delivered += 1

        errors.extend(self._notify_errors(event, tuple(errors)))
        result = PublishResult(
            matched=len(matched),
            delivered=delivered,
            failed=len(errors),
            errors=tuple(errors),
        )
        if self._actions.on_after is not None:
            try:
                self._actions.on_after(event, result)
            except Exception as exc:
                errors.append(exc)
                errors.extend(self._notify_errors(event, (exc,)))
                result = PublishResult(
                    matched=len(matched),
                    delivered=delivered,
                    failed=len(errors),
                    errors=tuple(errors),
                )
        with self._condition:
            self._delivered += delivered
            self._failed += len(errors)
            self._condition.notify_all()
        if errors and self._error_policy is ErrorPolicy.RAISE:
            raise ExceptionGroup("Event delivery failed", errors)
        return result

    def _invoke_executor_callback(self, callback: Callable[[T], None], event: T) -> None:
        previous = getattr(self._executor_context, "active", False)
        self._executor_context.active = True
        try:
            callback(event)
        finally:
            self._executor_context.active = previous

    def _notify_errors(
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
                action(EventErrorContext(event, error))
            except Exception as exc:
                action_errors.append(exc)
        return action_errors

    def _deliver(
        self,
        subscriber: _Subscriber[T],
        event: T,
    ) -> tuple[bool, Exception | None]:
        claimed, final = subscriber.claim()
        if not claimed:
            return False, None
        if final:
            subscriber.subscription._consume()
        try:
            subscriber.callback(event)
        except Exception as exc:
            return False, exc
        return True, None

    def publish_every(
        self,
        event: T,
        interval: float,
        *,
        times: int | None = None,
        immediately: bool = True,
    ) -> Subscription:
        """Publish the same event periodically until cancelled or completed."""

        interval = float(interval)
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("Event publish interval must be positive and finite")
        if times is not None and (
            isinstance(times, bool) or not isinstance(times, int) or times <= 0
        ):
            raise ValueError("Periodic publish times must be a positive integer")

        stopped = threading.Event()
        with self._lock:
            self._ensure_open()
            if len(self._schedules) >= self._max_schedules:
                raise EventBusError("Maximum periodic schedule capacity reached")
            schedule_id = self._next_id
            self._next_id += 1

            thread: threading.Thread | None = None

            def cancel() -> None:
                stopped.set()
                with self._lock:
                    if not self._closed:
                        self._schedules.pop(schedule_id, None)

            subscription = Subscription(schedule_id, cancel)

        def run() -> None:
            completed = 0
            try:
                if not immediately and stopped.wait(interval):
                    return
                while subscription.active and not self.closed:
                    try:
                        self.publish(event)
                    except Exception:
                        if self._error_policy is ErrorPolicy.RAISE:
                            break
                    completed += 1
                    if times is not None and completed >= times:
                        break
                    if not subscription.active:
                        break
                    if stopped.wait(interval):
                        break
            finally:
                subscription._deactivate()
                with self._lock:
                    self._schedules.pop(schedule_id, None)

        thread = threading.Thread(
            target=run,
            name=f"EventBusPeriodic-{schedule_id}",
            daemon=True,
        )
        with self._lock:
            self._schedules[schedule_id] = (subscription, thread)
            thread.start()
        return subscription

    def wait_for(
        self,
        *,
        event_filter: EventFilter[T] | None = None,
        event_type: EventType | None = None,
        predicate: Callable[[T], bool] | None = None,
        timeout: float | None = None,
    ) -> T | None:
        if timeout is not None and timeout < 0:
            raise ValueError("Event wait timeout cannot be negative")
        normalized_filter = coerce_event_filter(
            event_filter,
            event_type=event_type,
            predicate=predicate,
        )
        received: list[T] = []

        def capture(event: T) -> None:
            with self._condition:
                received.append(event)
                self._condition.notify_all()

        subscription = self.subscribe(
            capture,
            event_filter=normalized_filter,
            once=True,
        )
        timed_out = False
        try:
            with self._condition:
                ready = self._condition.wait_for(
                    lambda: bool(received) or self._closed,
                    timeout=timeout,
                )
                timed_out = not ready
                value = received[0] if ready and received else None
        finally:
            subscription.cancel()
        if timed_out and self._actions.on_timeout is not None:
            self._actions.on_timeout(EventTimeoutContext(normalized_filter, timeout))
        return value

    def clear(self) -> None:
        with self._lock:
            subscriptions = tuple(
                subscriber.subscription for subscriber in self._subscribers.values()
            )
            self._subscribers.clear()
        for subscription in subscriptions:
            subscription._deactivate()

    def close(self) -> None:
        with self._condition:
            if self._closed and not self._schedules:
                return
            self._closed = True
            subscriptions = tuple(
                subscriber.subscription for subscriber in self._subscribers.values()
            )
            schedules = tuple(self._schedules.values())
            self._subscribers.clear()
            self._condition.notify_all()
        for subscription in subscriptions:
            subscription._deactivate()
        for schedule, _thread in schedules:
            schedule.cancel()
        deadline = time.monotonic() + self._shutdown_timeout
        pending: list[str] = []
        for _schedule, thread in schedules:
            if thread is not threading.current_thread():
                thread.join(max(0.0, deadline - time.monotonic()))
                if thread.is_alive():
                    pending.append(thread.name)
        with self._lock:
            for schedule_id, (_schedule, thread) in tuple(self._schedules.items()):
                if not thread.is_alive():
                    self._schedules.pop(schedule_id, None)
        if pending:
            raise EventShutdownTimeoutError(
                "Periodic event schedules did not stop in time: " + ", ".join(pending)
            )

    def __len__(self) -> int:
        return self.subscriber_count

    def __enter__(self) -> "EventBus[T]":
        self._ensure_open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

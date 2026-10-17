"""Configurable callback registrations, independent of transport and event bus."""

from __future__ import annotations

import asyncio
import inspect
import math
import threading
import time
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .subscription import Subscription
from ..compatibility import ExceptionGroup
from .errors import CallbackTimeoutError


@dataclass(slots=True)
class CallbackContext:
    event: Any
    result: Any = None
    error: BaseException | None = None
    elapsed: float = 0.0


class CallbackSubscription(Subscription):
    """Callable decorator result with execution policy and per-registration hooks.

    Predicates run at delivery time. Rate-limited/disabled/filtered events do
    not consume max_calls. Sync timeout is observational, never thread killing.
    Hooks receive CallbackContext and run outside the registration lock.
    """

    def __init__(self, identifier: int, cancel: Callable[[], None], callback: Callable,
                 *, asynchronous: bool = False, once: bool = False,
                 max_calls: int | None = None, frequency_hz: float | None = None,
                 timeout: float | None = None, predicate: Callable | None = None,
                 enabled: bool = True, on_before: Callable | None = None,
                 on_success: Callable | None = None, on_error: Callable | None = None,
                 on_timeout: Callable | None = None, on_after: Callable | None = None) -> None:
        super().__init__(identifier, cancel)
        if once and max_calls is not None:
            raise ValueError("once and max_calls cannot be combined")
        if max_calls is not None and (isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls < 1):
            raise ValueError("max_calls must be a positive integer")
        for value in (frequency_hz, timeout):
            if value is not None and (isinstance(value, bool) or not math.isfinite(value) or value <= 0):
                raise ValueError("frequency_hz and timeout must be positive and finite")
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a bool")
        if predicate is not None and (not callable(predicate) or inspect.iscoroutinefunction(predicate)):
            raise TypeError("predicate must be a synchronous callable")
        self.callback = callback
        self.asynchronous = asynchronous
        self.timeout = timeout
        self.predicate = predicate
        self._enabled = enabled
        self._maximum = 1 if once else max_calls
        self._interval = 0.0 if frequency_hz is None else 1.0 / frequency_hz
        self._last = float("-inf")
        self._calls = 0
        self._policy_lock = threading.RLock()
        self._hooks: dict[str, list[Callable]] = {}
        for name, hook in (("before", on_before), ("success", on_success), ("error", on_error),
                           ("timeout", on_timeout), ("after", on_after)):
            if hook is not None:
                self._hook(name, hook)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.callback(*args, **kwargs)

    @property
    def enabled(self) -> bool:
        with self._policy_lock:
            return self._enabled

    def enable(self) -> None:
        with self._policy_lock:
            self._enabled = True

    def disable(self) -> None:
        with self._policy_lock:
            self._enabled = False

    def _hook(self, name: str, callback: Callable) -> Callable:
        asynchronous = inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(getattr(callback, "__call__", None))
        if not callable(callback) or asynchronous != self.asynchronous:
            raise TypeError("Hook must match callback sync/async mode")
        with self._policy_lock:
            self._hooks.setdefault(name, []).append(callback)
        return callback

    def on_before(self, callback: Callable) -> Callable:
        return self._hook("before", callback)

    def on_success(self, callback: Callable) -> Callable:
        return self._hook("success", callback)

    def on_error(self, callback: Callable) -> Callable:
        return self._hook("error", callback)

    def on_timeout(self, callback: Callable) -> Callable:
        return self._hook("timeout", callback)

    def on_after(self, callback: Callable) -> Callable:
        return self._hook("after", callback)

    def _accept(self, event: Any) -> bool:
        if not self.active or not self.enabled:
            return False
        if self.predicate is not None:
            accepted = self.predicate(event)
            if inspect.isawaitable(accepted):
                if inspect.iscoroutine(accepted):
                    accepted.close()
                raise TypeError("predicate returned an awaitable")
            if not accepted:
                return False
        with self._policy_lock:
            now = time.monotonic()
            if not self.active or not self._enabled or now - self._last < self._interval:
                return False
            if self._maximum is not None and self._calls >= self._maximum:
                return False
            self._calls += 1
            self._last = now
            exhausted = self._calls == self._maximum
        if exhausted:
            self.cancel()
        return True

    def _snapshot(self, name: str) -> tuple[Callable, ...]:
        with self._policy_lock:
            return tuple(self._hooks.get(name, ()))

    def _run_hooks(self, name: str, context: CallbackContext) -> None:
        errors = []
        for hook in self._snapshot(name):
            try:
                hook(context)
            except Exception as error:
                errors.append(error)
        self._raise_errors(errors)

    async def _run_hooks_async(self, name: str, context: CallbackContext) -> None:
        errors = []
        for hook in self._snapshot(name):
            try:
                await hook(context)
            except Exception as error:
                errors.append(error)
        self._raise_errors(errors)

    @staticmethod
    def _raise_errors(errors: list[Exception]) -> None:
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("Callback and hook failures", errors)

    def _report_failure(self, name: str, context: CallbackContext, error: Exception) -> None:
        context.error = error
        try:
            self._run_hooks(name, context)
        except Exception as hook_error:
            raise ExceptionGroup("Callback and error hook failed", [error, hook_error]) from None

    async def _report_failure_async(self, name: str, context: CallbackContext, error: Exception) -> None:
        context.error = error
        try:
            await self._run_hooks_async(name, context)
        except Exception as hook_error:
            raise ExceptionGroup("Callback and error hook failed", [error, hook_error]) from None

    def invoke(self, event: Any) -> Any:
        context = CallbackContext(event)
        started = time.monotonic()
        try:
            if not self._accept(event):
                return None
        except Exception as error:
            self._report_failure("error", context, error)
            raise
        try:
            self._run_hooks("before", context)
            context.result = self.callback(event)
            if inspect.isawaitable(context.result):
                if inspect.iscoroutine(context.result):
                    context.result.close()
                raise TypeError("Sync callback returned an awaitable")
            context.elapsed = time.monotonic() - started
            if self.timeout is not None and context.elapsed > self.timeout:
                raise CallbackTimeoutError("Callback exceeded its synchronous time budget")
            self._run_hooks("success", context)
        except Exception as error:
            self._report_failure("timeout" if isinstance(error, CallbackTimeoutError) else "error", context, error)
            raise
        else:
            return context.result
        finally:
            context.elapsed = time.monotonic() - started
            pending = sys.exc_info()[1]
            try:
                self._run_hooks("after", context)
            except Exception as error:
                failures = [pending] if isinstance(pending, Exception) else []
                try:
                    self._report_failure("error", context, error)
                except Exception as report_error:
                    failures.append(report_error)
                else:
                    failures.append(error)
                if pending is not None and not isinstance(pending, Exception):
                    raise pending from ExceptionGroup("Cleanup failed", failures)
                self._raise_errors(failures)

    async def invoke_async(self, event: Any) -> Any:
        context = CallbackContext(event)
        started = time.monotonic()
        try:
            if not self._accept(event):
                return None
        except Exception as error:
            await self._report_failure_async("error", context, error)
            raise
        try:
            await self._run_hooks_async("before", context)
            operation = self.callback(event)
            if self.timeout is None:
                context.result = await operation
            else:
                remaining = self.timeout - (time.monotonic() - started)
                if remaining <= 0:
                    if inspect.iscoroutine(operation):
                        operation.close()
                    raise CallbackTimeoutError("Callback exceeded its asynchronous time budget")
                task = asyncio.ensure_future(operation)
                done, _ = await asyncio.wait({task}, timeout=remaining)
                if task not in done:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise CallbackTimeoutError("Callback exceeded its asynchronous time budget")
                context.result = await task
            context.elapsed = time.monotonic() - started
            await self._run_hooks_async("success", context)
        except asyncio.CancelledError as error:
            context.error = error
            raise
        except Exception as error:
            await self._report_failure_async("timeout" if isinstance(error, CallbackTimeoutError) else "error", context, error)
            raise
        else:
            return context.result
        finally:
            context.elapsed = time.monotonic() - started
            pending = sys.exc_info()[1]
            try:
                await self._run_hooks_async("after", context)
            except Exception as error:
                failures = [pending] if isinstance(pending, Exception) else []
                try:
                    await self._report_failure_async("error", context, error)
                except Exception as report_error:
                    failures.append(report_error)
                else:
                    failures.append(error)
                if pending is not None and not isinstance(pending, Exception):
                    raise pending from ExceptionGroup("Cleanup failed", failures)
                self._raise_errors(failures)

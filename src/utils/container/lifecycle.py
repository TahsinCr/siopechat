"""Cached-resource ownership and deterministic disposal."""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Callable, Iterable
from typing import Any

from ..compatibility import ExceptionGroup
from .registration import MISSING, Provider
from .errors import AsyncDependencyError
from .resolution import close_awaitable, format_token, maybe_await


class ResourceTracker:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._creation_order: list[Any] = []
        self._identities: set[int] = set()
        self._disposing: set[int] = set()

    def remember(self, instance: Any) -> None:
        with self._lock:
            identity = id(instance)
            if instance is not MISSING and identity not in self._identities:
                self._creation_order.append(instance)
                self._identities.add(identity)

    def ordered(self, candidates: Iterable[Any]) -> tuple[Any, ...]:
        values = tuple(item for item in candidates if item is not MISSING)
        active_ids = {id(item) for item in values}
        ordered: list[Any] = []
        seen: set[int] = set()
        with self._lock:
            creation_order = tuple(self._creation_order)
        for item in reversed(creation_order):
            identity = id(item)
            if identity in active_ids and identity not in seen:
                ordered.append(item)
                seen.add(identity)
        for item in reversed(values):
            identity = id(item)
            if identity not in seen:
                ordered.append(item)
                seen.add(identity)
        return tuple(ordered)

    def forget(self, instances: Iterable[Any]) -> None:
        identities = {id(instance) for instance in instances}
        with self._lock:
            self._creation_order = [
                item for item in self._creation_order if id(item) not in identities
            ]
            self._identities.difference_update(identities)
            self._disposing.difference_update(identities)

    def resources(self) -> tuple[Any, ...]:
        """Return every resource still owned, including failed disposals."""

        with self._lock:
            return tuple(self._creation_order)

    def claim(self, instances: Iterable[Any]) -> tuple[Any, ...]:
        """Claim each owned resource for at most one concurrent disposal."""

        claimed: list[Any] = []
        with self._lock:
            for instance in instances:
                identity = id(instance)
                if identity in self._identities and identity not in self._disposing:
                    self._disposing.add(identity)
                    claimed.append(instance)
        return tuple(claimed)

    def release(self, instance: Any) -> None:
        with self._lock:
            self._disposing.discard(id(instance))


def cached_instances(provider: Provider) -> tuple[Any, ...]:
    instances: list[Any] = []
    if provider.instance is not MISSING:
        instances.append(provider.instance)
    if provider.singleton is not MISSING:
        instances.append(provider.singleton)
    return tuple(instances)


def requires_async_disposal(instance: Any) -> bool:
    close = getattr(instance, "close", None)
    if callable(close):
        return inspect.iscoroutinefunction(close)
    return callable(getattr(instance, "aclose", None))


def dispose(instance: Any) -> None:
    close = getattr(instance, "close", None)
    if not callable(close):
        if callable(getattr(instance, "aclose", None)):
            raise AsyncDependencyError(
                f"{format_token(type(instance))}.aclose() async; "
                "unregister_async/shutdown_async kullan."
            )
        return
    if inspect.iscoroutinefunction(close):
        raise AsyncDependencyError(
            f"{format_token(type(instance))}.close() async; "
            "unregister_async/shutdown_async kullan."
        )
    result = close()
    if inspect.isawaitable(result):
        close_awaitable(result)
        raise AsyncDependencyError(
            f"{format_token(type(instance))}.close() async; "
            "unregister_async/shutdown_async kullan."
        )


async def dispose_async(instance: Any) -> None:
    aclose = getattr(instance, "aclose", None)
    if callable(aclose):
        await maybe_await(aclose())
        return
    close = getattr(instance, "close", None)
    if callable(close):
        await maybe_await(close())


def dispose_many(
    instances: Iterable[Any],
    *,
    on_success: Callable[[Any], None] | None = None,
    on_failure: Callable[[Any], None] | None = None,
) -> None:
    errors: list[Exception] = []
    seen: set[int] = set()
    for instance in instances:
        if instance is MISSING or id(instance) in seen:
            continue
        seen.add(id(instance))
        try:
            dispose(instance)
        except Exception as exc:
            errors.append(exc)
            if on_failure is not None:
                on_failure(instance)
        else:
            if on_success is not None:
                on_success(instance)
    raise_disposal_errors(errors)


async def dispose_many_async(
    instances: Iterable[Any],
    *,
    on_success: Callable[[Any], None] | None = None,
    on_failure: Callable[[Any], None] | None = None,
) -> None:
    errors: list[Exception] = []
    cancellation: asyncio.CancelledError | None = None
    seen: set[int] = set()
    for instance in instances:
        if instance is MISSING or id(instance) in seen:
            continue
        seen.add(id(instance))
        try:
            await dispose_async(instance)
        except asyncio.CancelledError as exc:
            cancellation = exc
            if on_failure is not None:
                on_failure(instance)
        except Exception as exc:
            errors.append(exc)
            if on_failure is not None:
                on_failure(instance)
        else:
            if on_success is not None:
                on_success(instance)
    raise_disposal_errors(errors)
    if cancellation is not None:
        raise cancellation


def raise_disposal_errors(errors: Iterable[Exception]) -> None:
    collected = tuple(errors)
    if not collected:
        return
    if len(collected) == 1:
        raise collected[0]
    raise ExceptionGroup("Birden fazla dependency kaynağı kapatılamadı", collected)

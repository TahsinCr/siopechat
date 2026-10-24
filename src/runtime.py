"""Application services share an explicit registry, event bus and lifecycle."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Hashable

from utils.compatibility import ExceptionGroup
from utils.events import EventBus


@dataclass(frozen=True)
class ServiceChanged:
    owner: str
    name: str
    state: str


class Runtime:
    def __init__(self):
        self._services: dict[Hashable, Any] = {}
        self.events = EventBus(history=64)
        self._owners: dict[Hashable, str] = {}
        self._closed = False
        self._closing = False
        self._closing_task = None
        self._shutdown = []
        self.provide("application", "events", self.events)

    def provide(self, owner: str, name: Hashable, service: Any) -> None:
        if self._closing or self._closed:
            raise RuntimeError("runtime is closed")
        if not owner or not name or name in self._services:
            raise ValueError("service name must be unique and owner must be nonempty")
        self._services[name] = service
        self._owners[name] = owner
        self.events.publish(ServiceChanged(owner, str(name), "registered"))

    def get(self, name: Hashable) -> Any:
        try:
            return self._services[name]
        except KeyError:
            raise LookupError(f"service is unavailable: {name}") from None

    def _tokens(self, owner: str) -> tuple[Hashable, ...]:
        return tuple(name for name in reversed(self._owners) if self._owners[name] == owner)

    def unregister(self, owner: str) -> None:
        errors = []
        for name in self._tokens(owner):
            try:
                self._close_sync(self._services[name])
            except Exception as error:
                errors.append(error)
            else:
                del self._services[name]
                del self._owners[name]
                self.events.publish(ServiceChanged(owner, str(name), "removed"))
        self._raise_cleanup(errors)

    async def unregister_async(self, owner: str) -> None:
        errors = []
        for name in self._tokens(owner):
            try:
                await self._close_async(self._services[name])
            except Exception as error:
                errors.append(error)
            else:
                del self._services[name]
                del self._owners[name]
                self.events.publish(ServiceChanged(owner, str(name), "removed"))
        self._raise_cleanup(errors)

    @staticmethod
    def _close_sync(service: Any) -> None:
        close = getattr(service, "close", None)
        if not callable(close):
            if callable(getattr(service, "aclose", None)):
                raise RuntimeError("use asynchronous service cleanup")
            return
        if inspect.iscoroutinefunction(close):
            raise RuntimeError("use asynchronous service cleanup")
        result = close()
        if inspect.isawaitable(result):
            dispose = getattr(result, "close", None)
            if callable(dispose):
                dispose()
            raise RuntimeError("use asynchronous service cleanup")

    @staticmethod
    async def _close_async(service: Any) -> None:
        close = getattr(service, "aclose", None)
        if not callable(close):
            close = getattr(service, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result

    @staticmethod
    def _raise_cleanup(errors: list[Exception]) -> None:
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("service cleanup failed", errors)

    def on_shutdown(self, callback) -> None:
        self._shutdown.append(callback)

    async def aclose(self) -> None:
        if self._closed:
            return
        if self._closing_task is None:
            self._closing = True
            self._closing_task = asyncio.create_task(self._shutdown_services())
            self._closing_task.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        task = self._closing_task
        try:
            await asyncio.shield(task)
        finally:
            if task.done():
                self._closing_task = None

    async def _shutdown_services(self) -> None:
        failed = []
        errors = []
        while self._shutdown:
            callback = self._shutdown.pop()
            try:
                result = callback()
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                self._shutdown.append(callback)
                self._shutdown.extend(reversed(failed))
                raise
            except Exception as error:
                failed.append(callback)
                errors.append(error)
        self._shutdown.extend(reversed(failed))
        # Keep resolution available while a dependent service still needs cleanup.
        self._raise_cleanup(errors)
        for name in reversed(tuple(self._services)):
            try:
                await self._close_async(self._services[name])
            except Exception as error:
                errors.append(error)
            else:
                del self._services[name]
                del self._owners[name]
        self._raise_cleanup(errors)
        self._closed = True

    def close(self) -> None:
        if self._closed:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.aclose())
        else:
            raise RuntimeError("use await runtime.aclose() inside an event loop")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        await self.aclose()

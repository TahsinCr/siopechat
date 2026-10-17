"""Subscription ownership for synchronous and asynchronous event buses."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable


class Subscription:
    def __init__(self, subscription_id: int, cancel: Callable[[], None]) -> None:
        self._id = subscription_id
        self._cancel = cancel
        self._active = True
        self._lock = threading.Lock()

    @property
    def id(self) -> int:
        return self._id

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    def _deactivate(self) -> bool:
        with self._lock:
            if not self._active:
                return False
            self._active = False
            return True

    def cancel(self) -> None:
        self._consume()

    def _consume(self) -> bool:
        if not self._deactivate():
            return False
        self._cancel()
        return True

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *_args: object) -> None:
        self.cancel()


class AsyncSubscription:
    def __init__(
        self,
        subscription_id: int,
        cancel: Callable[[], Awaitable[None]],
    ) -> None:
        self._id = subscription_id
        self._cancel = cancel
        self._active = True
        self._lock = asyncio.Lock()

    @property
    def id(self) -> int:
        return self._id

    @property
    def active(self) -> bool:
        return self._active

    async def _deactivate(self) -> bool:
        async with self._lock:
            if not self._active:
                return False
            self._active = False
            return True

    async def cancel(self) -> None:
        await self._consume()

    async def _consume(self) -> bool:
        if not await self._deactivate():
            return False
        await self._cancel()
        return True

    async def __aenter__(self) -> "AsyncSubscription":
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.cancel()

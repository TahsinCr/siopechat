"""Host-independent account routing and lifecycle for the public Python API."""

import asyncio
from collections.abc import AsyncIterator

from .api import Capability, ProviderEvent, SendRequest
from .contracts import Backend
from .errors import FeatureUnavailable
from .registry import create_provider


class ProviderClient:
    """An async resource scope for independent account backends.

    account() exposes the full Backend contract: authentication, models, history,
    projects, sources, uploads and downloads. stream() routes a SendRequest using
    both provider_id and account_id; chat/project remote IDs remain provider-owned.
    Closing the client cancels its active streams and closes every owned backend.
    Use one instance per asyncio event loop; application persistence is external.
    """

    def __init__(self):
        self._accounts: dict[tuple[str, str], Backend] = {}
        self._streams: dict[asyncio.Task, int] = {}
        self._iterators = set()
        self._closed = False
        self._closing = None

    def add(self, backend: Backend) -> Backend:
        if self._closed:
            raise RuntimeError("provider client is closed")
        key = (backend.id, backend.account_id)
        if not all(key) or key in self._accounts:
            raise ValueError("provider/account identity must be nonempty and unique")
        self._accounts[key] = backend
        return backend

    def connect(self, provider_id: str, account_id: str = "default", **configuration) -> Backend:
        """Configure an account lazily; no browser/session request is made here."""
        if self._closed:
            raise RuntimeError("provider client is closed")
        if (provider_id, account_id) in self._accounts:
            raise ValueError("provider/account identity must be unique")
        return self.add(create_provider(provider_id, account_id, **configuration))

    def account(self, provider_id: str, account_id: str = "default") -> Backend:
        if self._closed:
            raise RuntimeError("provider client is closed")
        return self._accounts[provider_id, account_id]

    @property
    def accounts(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._accounts)

    async def stream(self, request: SendRequest) -> AsyncIterator[ProviderEvent]:
        backend = self.account(request.provider_id, request.account_id)
        if Capability.CHAT not in backend.capabilities:
            raise FeatureUnavailable(Capability.CHAT)
        task = asyncio.current_task()
        self._streams[task] = self._streams.get(task, 0) + 1
        iterator = backend.stream(request)
        self._iterators.add(iterator)
        try:
            async for event in iterator:
                yield event
        finally:
            try:
                await iterator.aclose()
            finally:
                self._iterators.discard(iterator)
                count = self._streams.get(task, 1) - 1
                if count:
                    self._streams[task] = count
                else:
                    self._streams.pop(task, None)

    async def close(self) -> None:
        if self._closing is None:
            self._closed = True
            caller = asyncio.current_task()

            async def finish():
                tasks = [task for task in self._streams if task is not caller]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await asyncio.gather(
                    *(iterator.aclose() for iterator in tuple(self._iterators)),
                    return_exceptions=True,
                )
                self._iterators.clear()
                self._streams.clear()
                backends, self._accounts = tuple(self._accounts.values()), {}
                results = await asyncio.gather(
                    *(backend.close() for backend in backends), return_exceptions=True
                )
                for result in results:
                    if isinstance(result, BaseException):
                        raise result

            self._closing = asyncio.create_task(finish())
        await asyncio.shield(self._closing)

    async def __aenter__(self):
        if self._closed:
            raise RuntimeError("provider client is closed")
        return self

    async def __aexit__(self, *_):
        await self.close()

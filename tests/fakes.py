"""Controllable offline providers shared by behavioral tests."""

import asyncio

from providers import Capability, ConversationBound, SendRequest, TextDelta


class ControlledProvider:
    capabilities = frozenset(Capability)

    def __init__(self, provider_id="gpt"):
        self.id = provider_id
        self.started = asyncio.Queue()
        self.releases = {}
        self.closed = set()
        self.calls = []
        self.failure = None
        self.failures = {}

    async def stream(self, request):
        self.calls.append(request)
        release = self.releases.setdefault(request.chat_id, asyncio.Event())
        await self.started.put(request.chat_id)
        try:
            yield ConversationBound(f"remote-{request.chat_id}")
            yield TextDelta("partial")
            await release.wait()
            failure = self.failures.get(request.chat_id, self.failure)
            if failure is not None:
                raise failure
            yield TextDelta(" response")
        finally:
            self.closed.add(request.chat_id)

    def release(self, chat_id):
        self.releases.setdefault(chat_id, asyncio.Event()).set()


def request(chat_id="one", provider="gpt", account="default", **kwargs):
    return SendRequest(provider, account, chat_id, "hello", **kwargs)

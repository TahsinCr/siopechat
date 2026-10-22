"""Portable API tests: no application modules, private profiles or network access."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from providers import (
    Capability,
    ConversationBound,
    ProviderClient,
    ReplaceText,
    SendRequest,
    create_provider,
)


class FakeBackend:
    id = "claude"
    capabilities = frozenset({Capability.CHAT})

    def __init__(self, account_id, *, wait=False):
        self.account_id, self.wait = account_id, wait
        self.started, self.stopped = asyncio.Event(), asyncio.Event()
        self.closed = 0
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        self.started.set()
        try:
            if self.wait:
                await asyncio.Event().wait()
            yield ConversationBound("remote-chat")
            yield ReplaceText(self.account_id, "final")
        finally:
            self.stopped.set()

    async def close(self):
        self.closed += 1


class PublicAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_routes_both_account_identity_and_conversation_project_context(self):
        async with ProviderClient() as client:
            first = client.add(FakeBackend("first"))
            second = client.add(FakeBackend("second"))
            request = SendRequest(
                "claude",
                "second",
                "local",
                "Hello",
                conversation_id="remote-chat",
                project_id="remote-project",
            )
            events = [event async for event in client.stream(request)]
            self.assertEqual(events[-1], ReplaceText("second", "final"))
            self.assertEqual(second.requests, [request])
            self.assertEqual(first.requests, [])
            self.assertIs(client.account("claude", "second"), second)
        self.assertEqual((first.closed, second.closed), (1, 1))
        await client.close()
        self.assertEqual(second.closed, 1)

    async def test_close_cancels_stream_before_closing_backend(self):
        client = ProviderClient()
        backend = client.add(FakeBackend("default", wait=True))

        async def consume():
            return [
                event
                async for event in client.stream(SendRequest("claude", "default", "local", "Hello"))
            ]

        task = asyncio.create_task(consume())
        await backend.started.wait()
        await asyncio.wait_for(client.close(), 2)
        self.assertTrue(task.cancelled())
        self.assertTrue(backend.stopped.is_set())
        self.assertEqual(backend.closed, 1)
        self.assertFalse(client._streams)
        with self.assertRaises(RuntimeError):
            client.account("claude")

    async def test_concurrent_close_retries_only_backend_that_failed_to_close(self):
        client = ProviderClient()
        flaky = FakeBackend("first")
        flaky.id = "flaky"
        healthy = FakeBackend("second")
        healthy.id = "healthy"
        flaky_attempts = 0

        async def fail_once():
            nonlocal flaky_attempts
            flaky_attempts += 1
            if flaky_attempts == 1:
                raise RuntimeError("transient close failure")
            flaky.closed += 1

        flaky.close = fail_once
        client.add(flaky)
        client.add(healthy)
        results = await asyncio.gather(client.close(), client.close(), return_exceptions=True)
        self.assertTrue(all(isinstance(result, RuntimeError) for result in results))
        self.assertEqual(flaky_attempts, 1)
        self.assertEqual(healthy.closed, 1)
        self.assertEqual(client.accounts, (("flaky", "first"),))
        with self.assertRaises(RuntimeError):
            client.account("flaky", "first")

        await client.close()
        self.assertEqual(flaky_attempts, 2)
        self.assertEqual(flaky.closed, 1)
        self.assertEqual(healthy.closed, 1)
        self.assertEqual(client.accounts, ())

    async def test_early_stop_in_caller_task_can_close_suspended_iterator(self):
        client = ProviderClient()
        backend = client.add(FakeBackend("default"))
        stream = client.stream(SendRequest("claude", "default", "local", "Hello"))
        await anext(stream)
        await client.close()
        await stream.aclose()
        self.assertTrue(backend.stopped.is_set())

    async def test_duplicate_identity_is_rejected_before_constructing_an_account(self):
        async with ProviderClient() as client:
            client.add(FakeBackend("default"))
            with patch("providers.client.create_provider") as factory:
                with self.assertRaises(ValueError):
                    client.connect("claude")
                factory.assert_not_called()

    async def test_all_bundled_factories_are_lazy_and_reject_wrong_account_send(self):
        with tempfile.TemporaryDirectory() as folder:
            async with ProviderClient() as client:
                for provider in ("chatgpt", "gemini", "claude"):
                    backend = client.add(
                        create_provider(
                            provider,
                            "personal",
                            credential_file=Path(folder) / provider / "session.json",
                        )
                    )
                    with self.assertRaises(ValueError):
                        await anext(
                            backend.stream(SendRequest(provider, "other", "local", "Hello"))
                        )
            self.assertEqual(list(Path(folder).rglob("session.json")), [])

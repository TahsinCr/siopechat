"""Notebook context routing without real account writes."""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from providers import SendRequest
from providers.gemini import GeminiProvider
from providers.gemini.notebooks import notebook_id
from providers.gemini.transport import NotebookTransport, notebook_client_type, notebook_transport


class NotebookStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_notebook_id_is_forwarded_as_native_context_once(self):
        async def stream(prompt, **options):
            yield SimpleNamespace(text="Answer", thoughts=None)

        chat = SimpleNamespace(
            cid="c_test", metadata=["c_test"], send_message_stream=Mock(side_effect=stream)
        )
        self.client = Mock(start_chat=Mock(return_value=chat), close=AsyncMock())
        notebooks = SimpleNamespace(
            get=AsyncMock(
                return_value=SimpleNamespace(
                    metadata={
                        "native": ["notebooks/abcdef", [None] * 12 + [[2, None, None, None, 3]]]
                    }
                )
            )
        )
        self.provider = GeminiProvider(Path("unused"), "test", notebooks=notebooks)
        self.provider._client = self.client
        self.addAsyncCleanup(self.provider.close)
        request = SendRequest("gemini", "test", "local", "Question", project_id="notebooks/abcdef")
        events = [event async for event in self.provider.stream(request)]
        self.assertTrue(events)
        self.assertNotIn("gem", self.client.start_chat.call_args.kwargs)
        self.provider.notebooks.get.assert_awaited_once_with("notebooks/abcdef")
        self.assertEqual(chat.send_message_stream.call_count, 1)

    def test_notebook_aliases_are_strictly_gemini_native(self):
        self.assertEqual(
            notebook_id("https://gemini.google.com/notebook/abcdef"), "notebooks/abcdef"
        )
        with self.assertRaises(ValueError):
            notebook_id("https://evil.test/notebook/abcdef")

    def test_transport_supplies_native_notebook_context_without_mutating_input(self):
        original = Mock()
        inner = [None] * 81
        inner[40] = [1]
        data = {"at": "not-a-secret", "f.req": json.dumps([None, json.dumps(inner)])}
        transport = NotebookTransport(original, "notebooks/abcdef", [2, None, None, None, 3])
        from enum import Enum

        class Endpoint(Enum):
            GENERATE = "https://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate"

        transport.stream(
            "POST",
            Endpoint.GENERATE,
            data=data,
        )
        sent = json.loads(json.loads(original.stream.call_args.kwargs["data"]["f.req"])[1])
        self.assertEqual(sent[19], "notebooks/abcdef")
        self.assertEqual(sent[40][0], 1)
        self.assertEqual(sent[40][13], [2, None, None, None, 3])
        self.assertIsNone(json.loads(json.loads(data["f.req"])[1])[19])

    async def test_no_data_timeout_cancels_and_closes_stream(self):
        import asyncio

        from providers.errors import ProviderError

        closed = []

        async def stalled(request):
            try:
                await asyncio.sleep(10)
                yield None
            finally:
                closed.append(True)

        provider = GeminiProvider(Path("unused"))
        provider._stream = stalled
        provider.stream_idle_timeout = 0.01
        with self.assertRaisesRegex(ProviderError, "120 saniye"):
            _ = [e async for e in provider.stream(None)]
        self.assertEqual(closed, [True])

    def test_transport_restores_session_on_failure(self):
        original = object()

        class Base:
            def __init__(self):
                self.client = original

            @property
            def _live_client(self):
                return self.client

        client = notebook_client_type(Base)()
        with self.assertRaisesRegex(RuntimeError, "stop"):
            with notebook_transport(client, "notebooks/abcdef", [2]):
                self.assertIsInstance(client._live_client, NotebookTransport)
                replacement = object()
                client.client = replacement
                self.assertIs(client._live_client.transport, replacement)
                raise RuntimeError("stop")
        self.assertIs(client._live_client, replacement)

    def test_transport_does_not_touch_other_requests_and_rejects_changed_envelope(self):
        from providers.errors import ProviderError

        original = Mock()
        transport = NotebookTransport(original, "notebooks/abcdef", [2])
        data = {"f.req": "unchanged"}
        transport.stream("POST", "https://gemini.google.com/other", data=data)
        self.assertIs(original.stream.call_args.kwargs["data"], data)
        with self.assertRaises(ProviderError):
            transport.stream(
                "POST",
                "https://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate",
                data=data,
            )
        self.assertEqual(original.stream.call_count, 1)

    async def test_guest_fallback_is_not_an_authenticated_account(self):
        import tempfile

        from providers.errors import AccountPaused

        with tempfile.TemporaryDirectory() as folder:
            cookies = Path(folder) / "cookies.json"
            cookies.write_text(json.dumps({"__Secure-1PSID": "fixture"}))
            client = SimpleNamespace(init=AsyncMock(), close=AsyncMock(), account_status=1016)
            provider = GeminiProvider(cookies, client_factory=Mock(return_value=client))
            with self.assertRaisesRegex(AccountPaused, "misafir"):
                await provider.authenticate()
            self.assertIsNone(provider._client)
            client.close.assert_awaited_once()

    async def test_continuous_partial_events_still_have_a_total_deadline(self):
        import asyncio

        from providers.errors import ProviderError

        closed = []

        async def endless(request):
            try:
                while True:
                    await asyncio.sleep(0.002)
                    yield object()
            finally:
                closed.append(True)

        provider = GeminiProvider(Path("unused"))
        provider._stream = endless
        provider.stream_total_timeout = 0.015
        with self.assertRaisesRegex(ProviderError, "10 dakikalık"):
            _ = [e async for e in provider.stream(None)]
        self.assertEqual(closed, [True])

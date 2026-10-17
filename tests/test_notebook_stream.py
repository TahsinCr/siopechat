"""Notebook context routing without real account writes."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from providers import SendRequest
from providers.gemini import GeminiProvider
from providers.gemini.notebooks import notebook_id


class NotebookStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_notebook_id_is_forwarded_as_native_context_once(self):
        async def stream(prompt, **options):
            yield SimpleNamespace(text="Answer", thoughts=None)

        chat = SimpleNamespace(
            cid="c_test", metadata=["c_test"], send_message_stream=Mock(side_effect=stream)
        )
        self.client = Mock(start_chat=Mock(return_value=chat), close=AsyncMock())
        self.provider = GeminiProvider(Path("unused"), "test")
        self.provider._client = self.client
        self.addAsyncCleanup(self.provider.close)
        request = SendRequest("gemini", "test", "local", "Question", project_id="notebooks/abcdef")
        events = [event async for event in self.provider.stream(request)]
        self.assertTrue(events)
        self.assertEqual(self.client.start_chat.call_args.kwargs["gem"], "notebooks/abcdef")
        self.assertEqual(chat.send_message_stream.call_count, 1)

    def test_notebook_aliases_are_strictly_gemini_native(self):
        self.assertEqual(
            notebook_id("https://gemini.google.com/notebook/abcdef"), "notebooks/abcdef"
        )
        with self.assertRaises(ValueError):
            notebook_id("https://evil.test/notebook/abcdef")

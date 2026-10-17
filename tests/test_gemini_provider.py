"""Gemini adapter contracts with no Google account traffic."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from providers import ConversationBound, ReplaceText, SendRequest
from providers.gemini import GeminiProvider


class GeminiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        cookie_file = Path(self.folder.name) / "cookies.json"
        cookie_file.write_text(json.dumps({"__Secure-1PSID": "fake-test-cookie"}))
        self.client = Mock()
        self.client.init = AsyncMock()
        self.client.close = AsyncMock()

        async def generate_content_stream(prompt, extended_thinking=False):
            yield None

        self.client.generate_content_stream = generate_content_stream
        self.client.list_models.return_value = [
            SimpleNamespace(
                model_name="dynamic",
                display_name="Account model",
                model_id="id",
                description="",
                is_available=True,
            )
        ]
        self.provider = GeminiProvider(cookie_file, "test", client_factory=lambda *_: self.client)
        self.addAsyncCleanup(self.provider.close)

    async def test_sdk_logging_is_quiet_without_removing_host_sinks(self):
        try:
            import gemini_webapi
            from gemini_webapi.utils.logger import logger as sdk_logger
            from loguru import logger
        except ImportError:
            self.skipTest("Gemini SDK requires Python 3.11+")
        records = []
        sink = logger.add(lambda message: records.append(message.record), level="TRACE")
        namespace = {"__name__": "gemini_webapi.client", "logger": sdk_logger}
        exec("def emit():\n    logger.info('quota test')", namespace)

        async def initialize(**kwargs):
            namespace["emit"]()

        self.client.init.side_effect = initialize
        provider = GeminiProvider(self.provider.cookie_file)
        try:
            with patch.object(gemini_webapi, "GeminiClient", return_value=self.client):
                await provider.models()
            self.assertFalse(any(r["name"].startswith("gemini_webapi") for r in records))
            # Disabling is namespace scoped; it must not remove the caller's sink.
            self.assertIn(sink, logger._core.handlers)
        finally:
            await provider.close()
            logger.remove(sink)
            logger.enable("gemini_webapi")

    async def test_models_use_account_names_and_real_extended_flag(self):
        catalog = await self.provider.models()
        self.assertEqual(catalog.models[0].id, "dynamic")
        self.assertEqual(
            [choice.id for choice in catalog.models[0].efforts], ["standard", "extended"]
        )
        await self.provider.models()
        self.assertEqual(self.client.init.await_count, 1)

    async def test_history_preserves_thoughts_before_final(self):
        self.client.read_chat = AsyncMock(
            return_value=SimpleNamespace(
                turns=[
                    SimpleNamespace(
                        role="model",
                        text="Answer",
                        model_output=SimpleNamespace(thoughts="Thought"),
                    ),
                    SimpleNamespace(role="user", text="Question", model_output=None),
                ]
            )
        )
        self.client.list_chats.return_value = [SimpleNamespace(cid="c_test", title="Demo")]
        chat = await self.provider.get_chat("c_test")
        self.assertEqual([m.role for m in chat.messages], ["user", "assistant", "assistant"])
        self.assertEqual(chat.messages[1].channel, "analysis")
        self.assertEqual(chat.origin.account_id, "test")

    async def test_stream_does_not_resend_and_passes_files_and_effort(self):
        async def stream(prompt, **kwargs):
            yield SimpleNamespace(text="Answer", thoughts="Summary")

        chat = SimpleNamespace(
            cid="c_test", metadata=["c_test"], send_message_stream=Mock(side_effect=stream)
        )
        self.client.start_chat.return_value = chat
        events = [
            event
            async for event in self.provider.stream(
                SendRequest(
                    "gemini",
                    "test",
                    "local",
                    "Question",
                    model="dynamic",
                    effort="extended",
                    attachments=({"path": "/tmp/fake-file"},),
                )
            )
        ]
        self.assertEqual(events[-1], ConversationBound("c_test"))
        self.assertEqual(events[0], ReplaceText("Summary", "analysis"))
        self.assertTrue(chat.send_message_stream.call_args.kwargs["extended_thinking"])
        self.assertEqual(chat.send_message_stream.call_count, 1)
        with self.assertRaises(ValueError):
            _ = [
                event
                async for event in self.provider.stream(
                    SendRequest("gemini", "other", "local", "Q")
                )
            ]

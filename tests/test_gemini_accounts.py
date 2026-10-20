"""Gemini account routing, archived responses and temporary-session cleanup."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from providers import ConversationBound, ReplaceText
from providers.gemini import GeminiProvider


class AccountNativeTests(unittest.TestCase):
    def test_native_result_passes_through_account_send_and_archive_and_temporary_cleanup(self):
        import tempfile

        from accounts import identity
        from bootstrap import create_application

        with tempfile.TemporaryDirectory() as folder:
            api = create_application(Path(folder))
            try:
                accounts = api.get("accounts")
                key = accounts.add("gemini", "Native fixture")
                provider = GeminiProvider(
                    Path("unused"),
                    key.split("/")[1],
                    notebooks=SimpleNamespace(cdp_url="http://127.0.0.1:9231"),
                )
                provider._connect = AsyncMock(side_effect=AssertionError("SDK must not send"))

                async def stream(request):
                    if not request.conversation_id:
                        self.assertEqual(request.project_id, "notebooks/abcdef")
                    yield ConversationBound("c_abcdef")
                    yield ReplaceText("Reasoning summary", "analysis")
                    yield ReplaceText("Answer", "final")

                provider._stream = stream
                provider._temporary["temporary_fixture"] = ["temporary_fixture"]
                accounts._providers[key] = provider
                delivered = []
                result = accounts.send(
                    key,
                    None,
                    "Question",
                    [],
                    delivered.append,
                    lambda _: None,
                    model="gemini-flash",
                    effort="standard",
                    project_id=identity(key, "notebooks/abcdef"),
                )
                self.assertIsNone(result["cache_warning"])
                record = accounts.cached(key, "c_abcdef")
                self.assertEqual(
                    [m["text"] for m in record["messages"]],
                    ["Question", "Reasoning summary", "Answer"],
                )
                self.assertEqual(record["project_id"], identity(key, "notebooks/abcdef"))
                self.assertEqual(delivered[-1]["text"], "Answer")
                accounts.send(
                    key,
                    result["conversation_id"],
                    "Follow-up",
                    [],
                    delivered.append,
                    lambda _: None,
                    model="gemini-flash",
                    effort="standard",
                )
                continued = accounts.cached(key, "c_abcdef")
                self.assertEqual(continued["project_id"], identity(key, "notebooks/abcdef"))
                self.assertEqual(len(continued["messages"]), 6)
                provider._connect.assert_not_awaited()
                accounts.callbacks(key)["forget_temporary"](identity(key, "temporary_fixture"))
                self.assertNotIn("temporary_fixture", provider._temporary)
            finally:
                api.runtime.close()

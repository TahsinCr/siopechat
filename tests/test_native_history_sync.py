"""Native read transport, passive startup and one synchronization command."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bootstrap import create_application
from providers.gemini import GeminiProvider
from providers.gemini.history import conversations, history
from providers.models import FeatureState, Model, ModelCatalog, Origin


class NativeHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_history_read_does_not_wait_for_the_composer_gate(self):
        import asyncio

        provider = GeminiProvider(Path("unused"))
        provider.rpc.call = AsyncMock(return_value=[[[]]])
        async with provider._gate:
            result = await asyncio.wait_for(provider.get_chat("c_abc"), 0.2)
        self.assertEqual(result.messages, ())

    async def test_notebooks_share_the_account_http_reader(self):
        provider = GeminiProvider(Path("unused"), "personal")
        provider.rpc.call = AsyncMock(return_value=[])
        self.assertEqual((await provider.notebooks.list()).items, ())
        provider.rpc.call.assert_awaited_once_with(
            "CNgdBe", [2, ["en"], 0, None, [2]], "/notebooks/view"
        )

    async def test_history_uses_only_the_owned_account_http_session(self):
        provider = GeminiProvider(Path("unused"))
        client = SimpleNamespace(
            _batch_execute=AsyncMock(
                return_value=SimpleNamespace(text='[["wrb.fr","hNvQHb","[[]]"]]')
            )
        )
        provider.rpc.connect = AsyncMock(return_value=client)
        with patch("asyncio.create_subprocess_exec", side_effect=AssertionError("browser")):
            self.assertEqual((await provider.get_chat("c_abc")).messages, ())
        self.assertEqual(client._batch_execute.call_args.kwargs["current_retry"], 0)

    def test_empty_pinned_page_is_valid_and_contract_errors_are_explicit(self):
        def origin(cid):
            return Origin("gemini", "a", cid)

        self.assertEqual(len(conversations([[], [None, None, [["c_abc", "Title"]]]], origin)), 1)
        with self.assertRaisesRegex(Exception, "sözleşmesi"):
            conversations([[None]], origin)

    def test_history_preserves_ids_published_thoughts_and_raw_media_metadata(self):
        candidate = ["candidate", ["Answer"]] + [None] * 35 + [[["Thought"]]]
        turn = [[None, "r_id"], None, [["Question"]], [[candidate]]]
        chat = history([[turn]], lambda cid: Origin("gemini", "a", cid), "c_abc")
        self.assertEqual([m.channel for m in chat.messages], [None, "analysis", None])
        self.assertEqual(chat.messages[-1].metadata["native_candidate"], candidate)
        self.assertEqual(chat.messages[0].id, "r_id:user")


class PassiveStartupTests(unittest.TestCase):
    def test_saved_accounts_use_cached_data_without_a_browser_probe(self):
        import time

        with tempfile.TemporaryDirectory() as folder:
            api = create_application(Path(folder))
            try:
                a = api.get("accounts")
                key = a.add("gemini", "Gemini")
                catalog = ModelCatalog(FeatureState.AVAILABLE, (Model("m", "Model"),))
                a._catalogs[key] = (time.monotonic(), catalog)
                a.archive().put(key, "conversation", "c_abc", {"messages": [], "title": "Cached"})
                with patch.object(
                    a, "browser_connected", side_effect=AssertionError("browser probe")
                ):
                    self.assertIs(a.models(key), catalog)
                    self.assertEqual(a.cached(key, "c_abc")["title"], "Cached")
            finally:
                api.runtime.close()

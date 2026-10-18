"""Native read transport, passive startup and one synchronization command."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bootstrap import create_application
from providers.gemini import GeminiProvider
from providers.gemini.history import conversations, history
from providers.models import FeatureState, Model, ModelCatalog, Origin


class NativeHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_reads_never_initialize_an_independent_cookie_session(self):
        provider = GeminiProvider(
            Path("unused"), notebooks=SimpleNamespace(cdp_url="http://127.0.0.1:9231")
        )
        provider.browser.authenticated = True
        provider._connect = AsyncMock(side_effect=AssertionError("stale SDK session"))
        provider.browser.read = AsyncMock(
            side_effect=[[[], [None, None, [["c_abc", "Title"]]]], [[]]]
        )
        self.assertEqual((await provider.list_chats()).items[0].title, "Title")
        self.assertEqual((await provider.get_chat("c_abc")).messages, ())
        provider._connect.assert_not_awaited()

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
    def test_saved_accounts_do_not_open_browser_for_background_cache_reads(self):
        with tempfile.TemporaryDirectory() as folder:
            api = create_application(Path(folder))
            try:
                a = api.get("accounts")
                key = a.add("gemini", "Gemini")
                cookie = a.directory(key) / "cookies.json"
                cookie.parent.mkdir(parents=True)
                cookie.write_text("{}")
                catalog = ModelCatalog(FeatureState.AVAILABLE, (Model("m", "Model"),))
                a._catalogs[key] = (0, catalog)
                a.archive().put(key, "conversation", "c_abc", {"messages": [], "title": "Cached"})
                fetch = Mock(side_effect=AssertionError("network"))
                with (
                    patch.object(a, "browser_connected", return_value=False),
                    patch.object(a, "ensure_browser") as launch,
                ):
                    self.assertIs(a.models(key), catalog)
                    self.assertEqual(
                        a.read_record(key, "conversation", "c_abc", fetch)["title"], "Cached"
                    )
                    self.assertEqual(a.read_record(key, "chats", "", fetch)["items"], [])
                    launch.assert_not_called()
                    fetch.assert_not_called()
            finally:
                api.runtime.close()

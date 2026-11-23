"""Native read transport, passive startup and one synchronization command."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from webaiprovider import GeminiWebAIProvider, WebAIAccount
from webaiprovider.providers.gemini.errors import GeminiCompatibilityError
from webaiprovider.providers.gemini.history import parse_chat_history, parse_chat_page

from accounts.shared import AccountCatalog, CatalogModel, ModelCatalogState
from bootstrap import create_application


class NativeHistoryTests(unittest.TestCase):
    def test_http_history_read_does_not_wait_for_the_composer_gate(self):
        provider = GeminiWebAIProvider(WebAIAccount("default", "gemini", Path("unused")))
        provider._opened = True
        provider.rpc.call = Mock(return_value=[[[]]])
        with provider._gate:
            result = provider.chats.get("c_abc")
        self.assertEqual(result.responses.list(), [])

    def test_notebooks_share_the_account_http_reader(self):
        provider = GeminiWebAIProvider(WebAIAccount("personal", "gemini", Path("unused")))
        provider._opened = True
        provider.rpc.call = Mock(return_value=[])
        self.assertEqual(provider.projects.list(), [])
        provider.rpc.call.assert_called_once_with(
            "CNgdBe", [2, ["en"], 0, None, [2]], "/notebooks/view", allow_empty=False
        )

    def test_history_uses_only_the_owned_account_http_session(self):
        provider = GeminiWebAIProvider(WebAIAccount("default", "gemini", Path("unused")))
        provider._opened = True
        client = SimpleNamespace(
            batch=Mock(return_value=SimpleNamespace(text='[["wrb.fr","hNvQHb","[[]]"]]'))
        )
        provider.rpc.connect = Mock(return_value=client)
        with patch("subprocess.Popen", side_effect=AssertionError("browser")):
            self.assertEqual(provider.chats.get("c_abc").responses.list(), [])

    def test_empty_pinned_page_is_valid_and_contract_errors_are_explicit(self):
        self.assertEqual(len(parse_chat_page([[], [None, None, [["c_abc", "Title"]]]])), 1)
        with self.assertRaisesRegex(GeminiCompatibilityError, "contract changed"):
            parse_chat_page([[None]])

    def test_general_chat_list_preserves_notebook_membership(self):
        rows = [
            ["c_project", "Notebook chat", None, None, None, None, None, "notebooks/abcdef"],
            ["c_plain", "General chat", None, None, None, None, None, None],
        ]
        chats = parse_chat_page([[None, None, rows]])
        self.assertEqual(chats[0].project.id, "notebooks/abcdef")
        self.assertIsNone(chats[1].project)
        self.assertTrue(all(c.metadata["project_membership_known"] for c in chats))

    def test_history_preserves_ids_published_thoughts_and_raw_media_metadata(self):
        candidate = ["candidate", ["Answer"]] + [None] * 35 + [[["Published thought"]]]
        activity = [None] * 8
        activity[1] = "Searching the web"
        activity[7] = [["favicon", "https://example.com/result", "Published result"]]
        candidate[37] = [["Published thought"], [activity]]
        turn = [[None, "r_id"], None, [["Question"]], [[candidate]]]
        chat = parse_chat_history([[turn]], "c_abc")
        self.assertEqual([m.channel for m in chat.messages], [None, "analysis", None])
        self.assertIn("Published thought", chat.messages[1].content[0].text)
        self.assertIn("Searching the web", chat.messages[1].content[0].text)
        self.assertEqual(chat.messages[-1].metadata["native_candidate"], candidate)
        self.assertEqual(
            chat.messages[-1].metadata["sources"],
            ({"title": "Published result", "url": "https://example.com/result"},),
        )
        self.assertEqual(chat.messages[0].id, "r_id:user")

    def test_published_thought_survives_sqlite_reload_and_transcript_render(self):
        import tempfile

        from tui.markdown import MessageRenderer, visible_chat_messages

        candidate = ["candidate", ["Answer"]] + [None] * 35 + [[["Published thought"]]]
        activity = [None] * 8
        activity[1] = "Searching the web"
        activity[7] = [["favicon", "https://example.com/result", "Published result"]]
        candidate[37] = [["Published thought"], [activity]]
        turn = [[None, "r_id"], None, [["Question"]], [[candidate]]]
        chat = parse_chat_history([[turn]], "c_abc")
        with tempfile.TemporaryDirectory() as folder:
            api = create_application(Path(folder))
            try:
                accounts = api.get("accounts")
                key = accounts.add("gemini", "Gemini")
                archive = api.get("archive")
                archive.put(key, "conversation", "c_abc", accounts.chat_record(key, chat))
                stored = archive.get(key, "conversation", "c_abc")
                visible = visible_chat_messages(stored["messages"])
                rendered, _ = MessageRenderer().transcript(visible, 80, show_reasoning=True)
            finally:
                api.runtime.close()

        self.assertIn("Düşünme özeti", rendered)
        self.assertIn("Published thought", rendered)
        self.assertIn("Searching the web", rendered)
        self.assertIn("Answer", rendered)


class PassiveStartupTests(unittest.TestCase):
    def test_saved_accounts_use_cached_data_without_a_browser_probe(self):
        with tempfile.TemporaryDirectory() as folder:
            api = create_application(Path(folder))
            try:
                a = api.get("accounts")
                key = a.add("gemini", "Gemini")
                catalog = AccountCatalog(ModelCatalogState.AVAILABLE, (CatalogModel("m", "Model"),))
                a._catalogs[key] = (0, catalog)
                a.archive().put(key, "conversation", "c_abc", {"messages": [], "title": "Cached"})
                with patch.object(
                    a, "browser_connected", side_effect=AssertionError("browser probe")
                ):
                    self.assertIs(a.models(key), catalog)
                    self.assertEqual(a.cached(key, "c_abc")["title"], "Cached")
            finally:
                api.runtime.close()

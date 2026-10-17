"""Account identity, cache isolation and terminal routing without real sessions."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from accounts import identity, origin
from bootstrap import create_application
from providers import ConversationBound, ReplaceText
from providers.models import Conversation, Origin, Page


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.api = create_application(Path(self.folder.name))
        self.addCleanup(self.api.runtime.close)
        self.accounts = self.api.get("accounts")
        self.gemini = self.accounts.add("gemini", "Personal Gemini")

    def test_identity_and_private_settings_separate_accounts(self):
        self.assertNotEqual(identity("chatgpt/default", "same"), identity(self.gemini, "same"))
        self.assertEqual(origin(identity(self.gemini, "a/b")), (self.gemini, "a/b"))
        self.accounts.select(self.gemini)
        config = self.api.settings.snapshot()
        self.assertEqual(config["accounts"]["active"], self.gemini)
        self.assertEqual(config["providers"]["default"], "gemini")
        self.assertNotIn("cookie", json.dumps(config))

    def test_late_session_result_does_not_change_another_account(self):
        from tests.test_cli_interaction import terminal

        with terminal(session_action=lambda action: {"action": action}) as (view, _):
            pending = []
            view._read_in_background = lambda *args, **kw: pending.append(args)
            view.session_dialog.perform("verify")
            read, success, _ = pending.pop()
            view.account_key = self.gemini
            view.session_state = {"account": "Gemini", "authenticated": False}
            view.session_action = lambda _: self.fail("must keep captured callback")
            self.assertEqual(read(), {"action": "verify"})
            success({"authenticated": True})
            self.assertFalse(view.session_state["authenticated"])
            self.assertFalse(view.session_dialog.loading)

    def test_default_account_honors_debug_browser_override(self):
        self.accounts.cdp_url = "http://127.0.0.1:9333"
        self.assertEqual(self.accounts.session("chatgpt/default").cdp_url, self.accounts.cdp_url)
        self.assertNotEqual(self.accounts.session(self.gemini).cdp_url, self.accounts.cdp_url)

    def test_mixed_list_filter_and_account_pagination(self):
        self.accounts.status = lambda _: {"saved_snapshot": True}
        for key in ("chatgpt/default", self.gemini):
            provider = Mock()
            provider.close = AsyncMock()
            provider.list_chats = AsyncMock(
                return_value=Page(
                    (Conversation(Origin(*key.split("/"), "same"), key),),
                    "50" if key.startswith("chatgpt") else None,
                )
            )
            self.accounts._providers[key] = provider
        page = self.accounts.list_chats()
        self.assertEqual(len({item["id"] for item in page["items"]}), 2)
        filtered = self.accounts.list_chats({"gemini"})
        self.assertEqual([item["provider_id"] for item in filtered["items"]], ["gemini"])
        self.accounts.list_chats(cursor=page["cursor"])
        self.accounts._providers["chatgpt/default"].list_chats.assert_awaited_with(
            cursor="50", limit=50
        )

    def test_new_accounts_never_import_legacy_chatgpt_conversations(self):
        legacy = Path(self.folder.name) / "legacy"
        (legacy / "conversations").mkdir(parents=True)
        (legacy / "active-conversation.json").write_text(json.dumps({"conversation_id": "gpt-id"}))
        (legacy / "conversations" / "gpt-id.json").write_text(
            json.dumps({"id": "gpt-id", "messages": []})
        )
        with patch("backend.legacy_data_dir", return_value=legacy):
            cache = self.accounts.cache(self.gemini)
            self.assertIsNone(cache.load_cached_conversation())
            self.assertIsNone(cache.load_cached_conversation("gpt-id"))
            # Even a pointer contaminated by the previous fallback cannot import GPT data.
            cache.set_active_conversation("gpt-id")
            self.assertIsNone(cache.load_cached_conversation())
            additional = self.accounts.add("chatgpt", "Second GPT")
            self.assertIsNone(self.accounts.cache(additional).load_cached_conversation())
            self.assertEqual(
                self.accounts.cache("chatgpt/default").load_cached_conversation()["id"], "gpt-id"
            )
            cache.cache_conversation(
                {"id": "c_gemini", "messages": [{"role": "assistant", "text": "Answer"}]}
            )
            self.assertEqual(cache.load_cached_conversation()["id"], "c_gemini")

    def test_send_cache_and_temporary_mode_are_account_scoped(self):
        async def stream(request):
            yield ReplaceText("Answer")
            yield ConversationBound("same")

        provider = Mock()
        provider.close = AsyncMock()
        provider.stream = stream
        self.accounts._providers[self.gemini] = provider
        result = self.accounts.send(self.gemini, None, "Q", [], Mock(), Mock())
        self.assertEqual(origin(result["conversation_id"])[0], self.gemini)
        cached = self.accounts.cached(self.gemini, "same")
        self.assertEqual(cached["provider_id"], "gemini")
        self.assertIsNone(self.accounts.cached("chatgpt/default", "same"))
        self.accounts.cache(self.gemini).conversation_cache_path("same").unlink()
        self.accounts.send(self.gemini, None, "Private", [], Mock(), Mock(), temporary=True)
        self.assertIsNone(self.accounts.cached(self.gemini, "same"))

    def test_cross_account_project_move_fails_before_provider_call(self):
        provider = Mock()
        self.accounts._providers[self.gemini] = provider
        with self.assertRaises(ValueError):
            self.accounts.callbacks(self.gemini)["move_conversation"](
                identity(self.gemini, "chat"), identity("chatgpt/default", "g-p-work")
            )
        provider.move_chat.assert_not_called()

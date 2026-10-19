"""Account identity, cache isolation and terminal routing without real sessions."""

import asyncio
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from accounts import Accounts, identity, origin
from bootstrap import create_application
from providers import ConversationBound, ReplaceText
from providers.errors import AccountPaused
from providers.models import Conversation, FeatureState, Model, ModelCatalog, Origin, Page


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.api = create_application(Path(self.folder.name))
        self.addCleanup(self.api.runtime.close)
        self.accounts = self.api.get("accounts")
        self.gemini = self.accounts.add("gemini", "Personal Gemini")

    def test_account_status_does_not_require_a_browser_process(self):
        with (
            patch.object(self.accounts, "browser_connected", side_effect=AssertionError("browser")),
            patch(
                "providers.chatgpt.session.BrowserSession.launch",
                side_effect=AssertionError("browser"),
            ),
        ):
            self.assertEqual(self.accounts.status(self.gemini)["transport"], "http")
            self.assertFalse(self.accounts.status(self.gemini)["authenticated"])

    def test_verification_survives_closed_browser_and_account_service_restart(self):
        path = self.accounts.directory(self.gemini) / "cookies.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        self.accounts._remember_verification(self.gemini)
        restarted = Accounts(self.api)
        with patch.object(restarted, "browser_connected", return_value=False):
            state = restarted.status(self.gemini)
            self.assertTrue(state["authenticated"])
            self.assertEqual(state["transport"], "http")
        path.unlink()
        self.assertFalse(restarted.status(self.gemini)["authenticated"])

    def test_transport_failure_preserves_verification_but_auth_rejection_invalidates_it(self):
        path = self.accounts.directory(self.gemini) / "cookies.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        self.accounts._remember_verification(self.gemini)
        provider = Mock()
        provider.models = AsyncMock(side_effect=RuntimeError("connection refused"))
        provider.close = AsyncMock()
        self.accounts._providers[self.gemini] = provider
        with self.assertRaisesRegex(RuntimeError, "connection refused"):
            self.accounts.models(self.gemini, force=True)
        self.assertTrue(self.accounts.status(self.gemini)["authenticated"])
        provider.models.side_effect = AccountPaused("Sign in required")
        with self.assertRaises(AccountPaused):
            self.accounts.models(self.gemini, force=True)
        self.assertFalse(self.accounts.status(self.gemini)["authenticated"])
        self.assertFalse(self.accounts._verification_path(self.gemini).exists())

    def test_saved_model_catalog_cannot_verify_an_account(self):
        path = self.accounts.directory(self.gemini) / "cookies.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        with patch.object(self.accounts, "browser_connected", return_value=True):
            state = self.accounts.status(self.gemini)
            self.assertFalse(state["authenticated"])
            self.assertEqual(state["transport"], "http")

    def test_catalog_single_flight_cache_and_account_local_disk_snapshot(self):
        catalog = ModelCatalog(FeatureState.AVAILABLE, (Model("gemini-test", "Gemini"),))

        async def discover():
            await asyncio.sleep(0.04)
            return catalog

        provider = Mock()
        provider.models = AsyncMock(side_effect=discover)
        provider.close = AsyncMock()
        self.accounts._providers[self.gemini] = provider
        with ThreadPoolExecutor(3) as pool:
            values = list(pool.map(lambda _: self.accounts.models(self.gemini), range(3)))
        self.assertEqual(provider.models.await_count, 1)
        self.assertTrue(all(v == catalog for v in values))
        self.assertEqual(self.accounts.models(self.gemini), catalog)
        self.assertEqual(provider.models.await_count, 1)
        self.accounts.models(self.gemini, force=True)
        self.assertEqual(provider.models.await_count, 2)
        fresh = Accounts(self.api)
        self.assertEqual(fresh.cached_models(self.gemini), catalog)
        self.assertIsNone(fresh.cached_models("chatgpt/default"))

    def test_failed_refresh_preserves_last_usable_model_catalog(self):
        good = ModelCatalog(FeatureState.AVAILABLE, (Model("m", "Model"),))
        provider = Mock()
        provider.models = AsyncMock(return_value=good)
        provider.close = AsyncMock()
        self.accounts._providers[self.gemini] = provider
        self.accounts.models(self.gemini)
        provider.models.return_value = ModelCatalog(FeatureState.UNKNOWN, reason="offline")
        with self.assertRaisesRegex(RuntimeError, "offline"):
            self.accounts.models(self.gemini, force=True)
        self.assertIs(self.accounts.cached_models(self.gemini), good)

    def test_read_timeout_releases_catalog_single_flight_for_retry(self):
        async def stalled():
            await asyncio.sleep(10)

        provider = Mock()
        provider.models = AsyncMock(side_effect=stalled)
        provider.close = AsyncMock()
        self.accounts._providers[self.gemini] = provider
        with patch("accounts.MODEL_TIMEOUT", 0.02):
            with self.assertRaises(TimeoutError):
                self.accounts.models(self.gemini)
        self.assertNotIn(self.gemini, self.accounts._catalog_tasks)
        provider.models = AsyncMock(
            return_value=ModelCatalog(FeatureState.AVAILABLE, (Model("m", "M"),))
        )
        self.assertEqual(self.accounts.models(self.gemini).models[0].id, "m")
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            self.accounts.call(asyncio.sleep(10), timeout=0.02)
        self.assertLess(time.monotonic() - started, 1)

    def test_history_timeout_isolated_from_other_account(self):
        async def stalled(**kwargs):
            await asyncio.sleep(10)

        self.accounts.status = lambda key: {"saved_snapshot": True}
        for key in (self.gemini, "chatgpt/default"):
            provider = Mock()
            provider.close = AsyncMock()
            provider.list_chats = (
                AsyncMock(side_effect=stalled)
                if key == self.gemini
                else AsyncMock(
                    return_value=Page((Conversation(Origin("chatgpt", "default", "chat"), "GPT"),))
                )
            )
            self.accounts._providers[key] = provider
        with patch("accounts.HISTORY_TIMEOUT", 0.02):
            page = self.accounts.list_chats()
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["errors"], ["Personal Gemini"])

    def test_identity_and_private_settings_separate_accounts(self):
        self.assertNotEqual(identity("chatgpt/default", "same"), identity(self.gemini, "same"))
        self.assertEqual(origin(identity(self.gemini, "a/b")), (self.gemini, "a/b"))
        self.accounts.select(self.gemini)
        config = self.api.settings.snapshot()
        self.assertEqual(config["accounts"]["active"], self.gemini)
        self.assertNotIn("default", config["providers"])
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
        self.accounts.cache(self.gemini).conversation_cache_path("same").unlink(missing_ok=True)
        self.accounts.send(self.gemini, None, "Private", [], Mock(), Mock(), temporary=True)
        # Removing the legacy JSON snapshot must not delete the durable archive.
        archived = self.accounts.cached(self.gemini, "same")
        self.assertEqual(archived["messages"][0]["text"], "Q")
        self.assertNotIn("Private", str(archived))

    def test_cross_account_project_move_fails_before_provider_call(self):
        provider = Mock()
        self.accounts._providers[self.gemini] = provider
        with self.assertRaises(ValueError):
            self.accounts.callbacks(self.gemini)["move_conversation"](
                identity(self.gemini, "chat"), identity("chatgpt/default", "g-p-work")
            )
        provider.move_chat.assert_not_called()

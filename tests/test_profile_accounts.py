"""Profile import and account-session update behavior."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from providers.models import SessionState, SessionStatus
from tests.account_fixture import configured_application


class ProfileAccountTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.api = configured_application(Path(self.folder.name))
        self.addCleanup(self.api.runtime.close)
        self.accounts = self.api.get("accounts")
        self.gemini = self.accounts.add("gemini", "Personal Gemini")

    def test_rejected_candidate_preserves_current_session_and_verification(self):
        path = self.accounts.credentials(self.gemini)
        path.save({"__Secure-1PSID": "previous"})
        self.accounts._remember_verification(self.gemini)
        candidate = Mock(
            authenticate=AsyncMock(side_effect=RuntimeError("rejected")), close=AsyncMock()
        )
        with (
            patch(
                "accounts.import_cookies", return_value=("brave-origin", {"__Secure-1PSID": "new"})
            ),
            patch("accounts.GeminiProvider", return_value=candidate),
        ):
            with self.assertRaisesRegex(RuntimeError, "rejected"):
                self.accounts.session_action(self.gemini, "verify_profile")
        self.assertEqual(path.load(), {"__Secure-1PSID": "previous"})
        self.assertTrue(self.accounts.status(self.gemini)["authenticated"])
        self.assertFalse(self.accounts._session_changes)
        candidate.close.assert_awaited_once()

    def test_success_saves_only_selected_account_after_remote_verification(self):
        path = self.accounts.credentials(self.gemini)
        old = Mock(close=AsyncMock())
        self.accounts._providers[self.gemini] = old
        self.accounts._catalogs[self.gemini] = "old catalog"
        candidate = Mock(
            authenticate=AsyncMock(return_value=SessionStatus(SessionState.AUTHENTICATED, True)),
            close=AsyncMock(),
        )
        with (
            patch(
                "accounts.import_cookies", return_value=("brave-origin", {"__Secure-1PSID": "new"})
            ),
            patch("accounts.GeminiProvider", return_value=candidate),
            patch(
                "providers.chatgpt.session.BrowserSession.launch",
                side_effect=AssertionError("browser"),
            ),
        ):
            state = self.accounts.session_action(self.gemini, "verify_profile")
        self.assertTrue(state["authenticated"])
        self.assertEqual(state["imported_browser"], "brave-origin")
        self.assertEqual(path.load(), {"__Secure-1PSID": "new"})
        self.assertNotIn(self.gemini, self.accounts._catalogs)
        self.assertFalse(self.accounts.credentials("chatgpt/default").exists())
        if os.name != "nt":
            self.assertEqual(self.accounts.repository.path.stat().st_mode & 0o777, 0o600)
        old.close.assert_awaited_once()

    def test_import_is_blocked_during_send_before_reading_profile(self):
        self.accounts._sending[self.gemini] = 1
        with patch("accounts.import_cookies") as read:
            with self.assertRaises(RuntimeError):
                self.accounts.session_action(self.gemini, "verify_profile")
        read.assert_not_called()

"""Profile import, account-session and provider bootstrap behavior."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from providers.models import SessionState, SessionStatus
from tests.account_fixture import configured_application
from tests.profile_support import cookie


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


class ProfileUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_button_triggers_common_async_action_and_refresh(self):
        from tests.cli_support import terminal

        with terminal(
            session_action=lambda action: {
                "authenticated": True,
                "imported_browser": "brave-origin",
            }
        ) as (view, _):
            pending = []
            view._read_in_background = lambda read, success, failure, **kw: pending.append(
                (read, success)
            )
            view.refresh_sidebar = Mock()
            view.refresh_provider_catalog = Mock()
            self.assertEqual(
                set(view.session_dialog.buttons), {"verify_profile", "logout", "remove"}
            )
            view.session_dialog.buttons["verify_profile"].handler()
            self.assertTrue(view.session_dialog.loading)
            read, success = pending.pop()
            success(read())
            self.assertFalse(view.session_dialog.loading)
            self.assertTrue(view.session_state["authenticated"])
            self.assertIn("brave-origin", view.session_dialog.message)
            view.refresh_sidebar.assert_called_once()
            view.refresh_provider_catalog.assert_called_once()

    async def test_chatgpt_profile_auth_never_contacts_debugging_browser(self):
        from providers.chatgpt import BrowserSession, ChatGPTProvider, SessionPaths
        from tests.fakes import FakeClient

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            session = BrowserSession(
                SessionPaths(root / "session.json", root / "cache", root / "profile")
            )
            client = FakeClient(session)
            provider = ChatGPTProvider(session, client_factory=lambda _: client)
            with (
                patch.object(session, "is_ready", side_effect=AssertionError("CDP probe")),
                patch.object(
                    session, "capture_auth", side_effect=AssertionError("browser capture")
                ),
            ):
                status = await provider.authenticate(capture_browser=False)
            self.assertEqual(status.state, SessionState.AUTHENTICATED)
            self.assertTrue(client.closed.is_set())
            await provider.close()

    async def test_profile_action_is_visible_in_resized_accounts_panel(self):
        import asyncio

        from tests.cli_support import ResizableOutput, immediate, terminal, wait_until

        output = ResizableOutput()
        with terminal(output=output, session_action=lambda _: {}) as (view, _):
            view._read_in_background = immediate
            run = asyncio.create_task(view.app.run_async())
            try:
                await wait_until(lambda: view.app.is_running)
                view.settings_dialog.open("accounts")
                for columns, rows in ((100, 30), (80, 24), (60, 18), (40, 12)):
                    output.columns, output.rows = columns, rows
                    view.app.layout.focus(view.session_dialog.buttons["verify_profile"])
                    view.app.invalidate()
                    await asyncio.sleep(0.06)
                    screen = view.app.renderer._last_screen
                    text = "\n".join(
                        "".join(screen.data_buffer[y][x].char for x in range(columns))
                        for y in range(rows)
                    )
                    for buttons in (
                        [
                            view.session_dialog.add_account_button,
                            view.session_dialog.edit_account_button,
                            view.session_dialog.buttons["remove"],
                        ],
                        [
                            view.session_dialog.buttons["verify_profile"],
                            view.session_dialog.buttons["logout"],
                        ],
                    ):
                        labels = [button.text for button in buttons]
                        # The settings viewport may scroll either row off-screen.
                        visible = [line for line in text.splitlines() if labels[0] in line]
                        if visible:
                            self.assertTrue(
                                any(all(label in line for label in labels) for line in visible)
                            )
                    self.assertNotIn("Window too small", text)
                    self.assertNotIn("Bağlı tarayıcı", text)
                    self.assertNotIn("Tarayıcıda aç", text)
            finally:
                view.app.exit()
                await run


class ChatGPTProfileBootstrapTests(unittest.TestCase):
    def test_bootstrap_saves_provider_token_and_browser_headers_without_cdp(self):
        from unittest.mock import MagicMock

        from providers.chatgpt import BrowserSession, SessionPaths

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            session = BrowserSession(
                SessionPaths(root / "session.json", root / "cache", root / "profile")
            )
            http = MagicMock()
            http.get.return_value.status_code = 200
            http.get.return_value.json.return_value = {"accessToken": "verified-token"}
            http.cookies.jar = [
                cookie("__cf_bm", "fresh", domain=".chatgpt.com"),
                cookie("foreign", "ignore", domain=".evil.test"),
            ]
            factory = MagicMock()
            factory.return_value.__enter__.return_value = http
            with (
                patch("curl_cffi.requests.Session", factory),
                patch.object(session, "is_ready", side_effect=AssertionError("CDP")),
            ):
                session.import_cookies(
                    {"__Secure-next-auth.session-token": "login", "oai-did": "device"}
                )
            url, headers = session.load()
            self.assertEqual(url, "https://chatgpt.com/backend-api/conversations")
            self.assertEqual(headers["authorization"], "Bearer verified-token")
            self.assertIn("Chrome/", headers["user-agent"])
            self.assertEqual(headers["oai-device-id"], "device")
            self.assertIn("__cf_bm=fresh", headers["cookie"])
            self.assertNotIn("foreign", headers["cookie"])
            self.assertFalse(http.get.call_args.kwargs["allow_redirects"])
            self.assertEqual(http.get.call_args.args[0], "https://chatgpt.com/api/auth/session")

    def test_bootstrap_rejection_retains_previous_snapshot_and_does_not_follow_redirect(self):
        from unittest.mock import MagicMock

        from providers.chatgpt import BrowserSession, SessionPaths
        from providers.errors import ProviderError

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            session = BrowserSession(
                SessionPaths(root / "session.json", root / "cache", root / "profile")
            )
            session.save(
                "https://chatgpt.com/backend-api/conversations", {"authorization": "previous"}
            )
            before = session.paths.session_file.read_bytes()
            http = MagicMock()
            http.get.return_value.status_code = 302
            factory = MagicMock()
            factory.return_value.__enter__.return_value = http
            with patch("curl_cffi.requests.Session", factory), self.assertRaises(ProviderError):
                session.import_cookies({"__Secure-next-auth.session-token": "login"})
            self.assertEqual(session.paths.session_file.read_bytes(), before)
            http.get.assert_called_once()

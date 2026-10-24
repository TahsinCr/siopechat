"""Terminal account profile actions and responsive account controls."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from providers.models import SessionState


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

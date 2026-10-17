"""Mouse, settings, session, history and security regression coverage."""

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from prompt_toolkit.data_structures import Point, Size
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output import DummyOutput

from backend import BackendService
from bootstrap import create_application
from providers.chatgpt import BrowserSession, ChatGPTClient, SessionPaths
from providers.chatgpt.errors import ChatGPTWebError
from providers.models import Choice, FeatureState, Model, ModelCatalog
from tests import test_cli_interaction as interaction

terminal = interaction.terminal


def immediate(read, success, failure, **kwargs):
    try:
        success(read())
    except Exception as error:
        failure(str(error))


def click(fragment):
    return fragment[2](
        MouseEvent(Point(0, 0), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset())
    )


class UiControlsTests(unittest.TestCase):
    def test_clickable_tabs_sidebar_palette_and_scroll_controls(self):
        with terminal() as (view, _):
            view.new_tab()
            click(view.render_tabs()[2])
            self.assertEqual(view.active_tab, 0)
            view.sidebar_items = [{"id": "chat", "title": "Example"}]
            view._build_sidebar_rows()
            click(view.render_sidebar()[0])
            self.assertIn("section:projects", view.sidebar_collapsed)
            view.open_palette()
            click(view.render_palette()[0])
            self.assertEqual(len(view.tabs), 3)
            click(view.render_scrollbar()[0])
            self.assertFalse(view.follow_output)
            click(view.render_scrollbar()[2])
            self.assertTrue(view.follow_output)
            self.assertTrue(view.app.mouse_support())

    def test_history_pagination_merges_and_stops_at_end(self):
        pages = Mock(return_value={"items": [{"id": "two", "title": "Older"}], "cursor": None})
        with terminal(list_history_page=pages) as (view, _):
            view._read_in_background = immediate
            view.sidebar_items = [{"id": "one", "title": "Latest"}]
            view.history_cursor = "50"
            view.load_more_history()
            pages.assert_called_once_with("50")
            self.assertEqual([item["id"] for item in view.sidebar_items], ["one", "two"])
            self.assertIsNone(view.history_cursor)
            self.assertFalse(view.history_loading)

    def test_transcript_status_redraw_reuses_complete_render(self):
        with terminal(initial_messages=[{"role": "assistant", "text": "Hello"}]) as (view, _):
            with patch.object(view.renderer, "render", wraps=view.renderer.render) as render:
                first = view.render_transcript()
                view.status = "New status"
                self.assertEqual(view.render_transcript(), first)
                self.assertEqual(render.call_count, 1)
                view.messages[0]["text"] = "Changed"
                self.assertNotEqual(view.render_transcript(), first)
                self.assertEqual(render.call_count, 2)

    def test_settings_dialog_uses_real_schema_persists_and_applies_preferences(self):
        with tempfile.TemporaryDirectory() as folder:
            api = create_application(Path(folder))
            self.addCleanup(api.runtime.close)

            def preferences():
                config = api.settings.snapshot()
                return {
                    "settings": config,
                    "translations": api.languages.merged(config["application"]["language"]),
                    "tokens": api.themes.resolve(config["application"]["theme"]),
                }

            def change(section, key, value):
                asyncio.run(
                    api.actions.invoke("settings.change", section=section, key=key, value=value)
                )
                return preferences()

            with terminal(
                settings_rows=lambda: asyncio.run(api.actions.invoke("settings.show")),
                change_setting=change,
            ) as (view, _):
                view._read_in_background = immediate
                view.settings_dialog.open()
                dialog = view.settings_dialog
                dialog.search.text = "application mouse"
                self.assertEqual(dialog.current["key"], "mouse")
                dialog.choose(False)
                dialog.save()
                self.assertFalse(view.mouse_enabled)
                self.assertFalse(json.loads(api.settings.path.read_text())["application"]["mouse"])
                dialog.search.text = "application language"
                dialog.choose("tr")
                dialog.save()
                self.assertEqual(view.text("settings", "fallback"), "Ayarlar")

    def test_logout_requires_confirmation_clears_ui_but_does_not_call_browser(self):
        action = Mock(
            side_effect=lambda name: (
                {"logged_out": True} if name == "logout" else {"saved_snapshot": True}
            )
        )
        with terminal(
            session_action=action, initial_messages=[{"role": "user", "text": "private"}]
        ) as (view, _):
            view._read_in_background = immediate
            view.session_dialog.open()
            view.session_dialog.logout()
            self.assertEqual(action.call_count, 1)
            view.session_dialog.logout()
            action.assert_called_with("logout")
            self.assertEqual(view.messages, [])
            self.assertFalse(view.session_state["authenticated"])
            self.assertTrue(view.session_dialog.visible)

    def test_role_headers_are_distinct_and_prompt_blocks_are_gone(self):
        with terminal() as (view, _):
            user = view.renderer.render("user", "A question", 60, False, False)
            assistant = view.renderer.render("assistant", "An answer", 60, True, False)
            self.assertIn("48;2;48;48;48", user)
            self.assertNotIn("ASİSTAN", user)
            self.assertIn("48;2;24;24;24", assistant)
            self.assertNotIn("48;2;75;75;75", user)
            self.assertNotIn("─" * 10, user)

    def test_model_and_effort_use_separate_semantic_colors(self):
        with terminal() as (view, _):
            view.catalog = ModelCatalog(
                FeatureState.AVAILABLE, (Model("demo", "Example", (Choice("medium", "Orta"),)),)
            )
            view.model, view.effort = "demo", "medium"
            fragments = view.render_chat_header()
            self.assertIn(
                "Example", next(f[1] for f in fragments if f[0] == "class:metadata.model")
            )
            self.assertIn("Orta", next(f[1] for f in fragments if f[0] == "class:metadata.effort"))
            style = view.app._merged_style
            self.assertEqual(style.get_attrs_for_style_str("class:metadata.model").color, "f0ad66")
            self.assertEqual(style.get_attrs_for_style_str("class:metadata.effort").color, "86d49b")

    def test_session_change_waits_for_uploads_and_background_actions(self):
        callback = Mock(return_value={})
        with terminal(session_action=callback) as (view, _):
            view._read_in_background = immediate
            for flag in ("uploading_image", "background_jobs"):
                setattr(view, flag, True)
                view.session_dialog.perform("logout")
                callback.assert_not_called()
                setattr(view, flag, False)

    def test_logout_discards_queued_results_and_sensitive_render_cache(self):
        with terminal(initial_messages=[{"role": "user", "text": "private"}]) as (view, _):
            view.render_transcript()
            pending = []
            dispatched = threading.Event()

            def dispatch(callback):
                pending.append(callback)
                dispatched.set()

            view._dispatch_ui = dispatch
            success = Mock()
            view._read_in_background(lambda: "old session result", success, Mock())
            self.assertTrue(dispatched.wait(1))
            view.after_logout()
            pending[0]()
            success.assert_not_called()
            self.assertEqual(view.renderer.cache, {})
            self.assertEqual(view.formatted_transcript_source, "")
            self.assertIsNone(view._transcript_signature)

    def test_setting_choices_cycle_without_saving_or_changing_selection(self):
        rows = [
            {
                "section": "application",
                "key": "theme",
                "title": "Theme",
                "value": "default",
                "choices": ["default", "custom"],
            }
        ]
        with terminal(settings_rows=lambda: rows) as (view, _):
            view._read_in_background = immediate
            view.settings_dialog.open()
            view.settings_dialog.cycle_choice(1)
            self.assertEqual(view.settings_dialog.editor.text, "custom")
            self.assertEqual(rows[0]["value"], "default")


class DialogInputTests(unittest.IsolatedAsyncioTestCase):
    settle = interaction.InputTests.settle

    async def test_settings_panel_edits_backspace_and_allows_new_chat_navigation(self):
        rows = [{"section": "application", "key": "model", "value": "abc", "title": "Model"}]
        with terminal(
            settings_rows=lambda: rows, change_setting=lambda *args: {"application": {}}
        ) as (view, pipe):
            view._read_in_background = immediate
            run = asyncio.create_task(view.app.run_async())
            try:
                await self.settle(lambda: view.app.is_running)
                pipe.send_bytes(b"\x1b[15~")
                await self.settle(lambda: view.settings_dialog.visible)
                pipe.send_bytes(b"\r")
                await self.settle(
                    lambda: view.app.layout.current_control is view.settings_dialog.editor.control
                )
                pipe.send_bytes(b"\x7f")
                await self.settle(lambda: view.settings_dialog.editor.text == "ab")
                pipe.send_bytes(b"\x14")
                await asyncio.sleep(0.05)
                self.assertEqual(len(view.tabs), 3)
                self.assertEqual(view.tab_kind, "chat")
                view.settings_dialog.close()
                self.assertIs(view.app.layout.current_control, view.input.control)
            finally:
                if view.app.is_running:
                    view.app.exit()
                await run

    async def test_bottom_metadata_and_uniform_background_after_sidebar_toggles(self):
        class Output(DummyOutput):
            size = Size(rows=24, columns=80)

            def get_size(self):
                return self.size

        output = Output()
        with terminal(
            output=output,
            initial_messages=[
                {"role": "user", "text": "Question"},
                {"role": "assistant", "text": "Answer"},
            ],
        ) as (view, _):
            view.catalog = ModelCatalog(
                FeatureState.AVAILABLE, (Model("demo", "Example", (Choice("medium", "Orta"),)),)
            )
            view.model, view.effort = "demo", "medium"
            run = asyncio.create_task(view.app.run_async())
            try:
                await self.settle(lambda: view.app.renderer._last_screen is not None)
                for columns in (80, 120, 60, 100):
                    output.size = Size(rows=24, columns=columns)
                    view.toggle_sidebar()
                    await asyncio.sleep(0.12)
                    screen = view.app.renderer._last_screen

                    def line(y):
                        return "".join(screen.data_buffer[y][x].char for x in range(columns))

                    self.assertIn("Example", line(22))
                    self.assertIn("Orta", line(22))
                    rendered = "\n".join(line(y) for y in range(24))
                    self.assertIn("Question", rendered)
                    self.assertIn("Answer", rendered)
                    self.assertNotIn("Window too small", rendered)
                    start = view.sidebar_width() + 1 if view.sidebar_visible else 0
                    for x in range(start, columns - 1):
                        attrs = view.app._merged_style.get_attrs_for_style_str(
                            screen.data_buffer[15][x].style
                        )
                        self.assertEqual(attrs.bgcolor, "202020")
            finally:
                if view.app.is_running:
                    view.app.exit()
                await run

    async def test_changed_keybinding_is_applied_without_restart(self):
        with terminal() as (view, pipe):
            run = asyncio.create_task(view.app.run_async())
            try:
                await self.settle(lambda: view.app.is_running)
                view.apply_preferences(
                    {"settings": {"application": {}, "keybindings": {"ui.command_palette": "f11"}}}
                )
                pipe.send_bytes(b"\x1b[23~")
                await self.settle(lambda: view.palette_visible)
                self.assertEqual(view.keybindings["ui.command_palette"], "f11")
            finally:
                if view.app.is_running:
                    view.app.exit()
                await run

    async def test_mouse_handler_from_rendered_toolbar_and_compact_dialogs(self):
        class Output(DummyOutput):
            size = Size(rows=24, columns=80)

            def get_size(self):
                return self.size

        output = Output()
        with terminal(output=output, session_action=lambda _: {}, settings_rows=lambda: []) as (
            view,
            _,
        ):
            view._read_in_background = immediate
            run = asyncio.create_task(view.app.run_async())
            try:
                await self.settle(lambda: view.app.renderer._last_screen is not None)
                handlers = view.app.renderer.mouse_handlers.mouse_handlers
                handlers[0][2](
                    MouseEvent(Point(2, 0), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset())
                )
                self.assertFalse(view.sidebar_visible)
                for columns, rows in ((80, 24), (60, 18), (120, 40)):
                    output.size = Size(rows=rows, columns=columns)
                    view.session_dialog.open()
                    view.app.invalidate()
                    await asyncio.sleep(0.12)
                    screen = view.app.renderer._last_screen
                    text = "\n".join(
                        "".join(cell.char for _, cell in sorted(row.items()))
                        for _, row in sorted(screen.data_buffer.items())
                    )
                    self.assertNotIn("Window too small", text)
                    self.assertIn("Oturumu doğrula", text)
                    view.session_dialog.close()
                    view.settings_dialog.open()
                    await asyncio.sleep(0.12)
                    screen = view.app.renderer._last_screen
                    text = "\n".join(
                        "".join(cell.char for _, cell in sorted(row.items()))
                        for _, row in sorted(screen.data_buffer.items())
                    )
                    self.assertNotIn("Window too small", text)
                    self.assertIn("Kaydet", text)
                    view.settings_dialog.close()
            finally:
                if view.app.is_running:
                    view.app.exit()
                await run


class SessionSecurityTests(unittest.TestCase):
    def test_saved_session_preferred_without_capturing_browser(self):
        session = Mock(cdp_url="http://127.0.0.1:9222")
        session.load.return_value = (
            "https://chatgpt.com/backend-api/conversations",
            {"authorization": "secret"},
        )
        with ChatGPTClient(session):
            session.capture_auth.assert_not_called()
            session.is_ready.assert_not_called()

    def test_authenticated_requests_cannot_escape_provider_origin(self):
        with ChatGPTClient(Mock(cdp_url="http://127.0.0.1:9222"), authenticate=False) as client:
            with patch.object(client.http, "request") as request:
                for target in (
                    "http://chatgpt.com/backend-api/models",
                    "https://example.com/",
                    "https://chatgpt.com@evil.example/",
                ):
                    with self.assertRaises(ChatGPTWebError):
                        client.request("GET", target)
                request.assert_not_called()

    def test_default_browser_does_not_claim_session_import(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            session = BrowserSession(
                SessionPaths(root / "session", root / "cache", root / "profile")
            )
            with patch("providers.chatgpt.session.webbrowser.open", return_value=True) as opened:
                result = session.open_login()
            opened.assert_called_once_with("https://chatgpt.com/", new=2)
            self.assertFalse(result["authenticated"])
            self.assertFalse(session.paths.session_file.exists())

    def test_invalid_session_utf8_and_malformed_ipc_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            session = BrowserSession(
                SessionPaths(root / "session", root / "cache", root / "profile")
            )
            session.paths.session_file.write_bytes(b"\xff")
            with self.assertRaises(ChatGPTWebError):
                session.load()
            session.paths.session_file.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
            with self.assertRaises(ChatGPTWebError):
                session.load()
        connection = Mock()
        connection.recv.side_effect = [b"\xff", b""]
        with self.assertRaises(ChatGPTWebError):
            BackendService().receive_json(connection)

    def test_history_ipc_validates_bounds_and_propagates_cursor(self):
        backend = BackendService()
        client = Mock()
        client.list_page.return_value = {"items": [{"id": str(i)} for i in range(50)]}
        result, _ = backend.handle_daemon_command(
            client, {"command": "history_page", "offset": 50, "limit": 50}
        )
        self.assertEqual(result["cursor"], "100")
        client.list_page.return_value["total"] = 100
        result, _ = backend.handle_daemon_command(
            client, {"command": "history_page", "offset": 50, "limit": 50}
        )
        self.assertIsNone(result["cursor"])
        for args in ({"offset": -1}, {"limit": 101}, {"limit": True}):
            with self.assertRaises(ChatGPTWebError):
                backend.handle_daemon_command(client, {"command": "history_page", **args})

    def test_logout_stops_daemon_before_removing_session(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "session.json"
            path.write_text("session")
            backend = BackendService()
            with (
                patch.object(backend, "auth_store_path", return_value=path),
                patch.object(backend, "daemon_is_ready", side_effect=[True, False]),
                patch.object(backend, "daemon_request") as request,
            ):
                backend.forget_session(Path(folder) / "socket")
                request.assert_called_once()
            self.assertFalse(path.exists())

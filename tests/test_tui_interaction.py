"""Keyboard, input and navigation behavior through Prompt Toolkit."""

import asyncio
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

from prompt_toolkit.data_structures import Size
from prompt_toolkit.mouse_events import MouseButton, MouseEventType
from prompt_toolkit.output import DummyOutput

from tests.tui_support import immediate, screen_text, terminal, wait_until


class InputTests(unittest.IsolatedAsyncioTestCase):
    settle = staticmethod(wait_until)

    async def test_transcript_home_end_share_history_actions_but_composer_keeps_editing(self):
        with terminal(initial_messages=[{"role": "user", "text": "Prompt"}]) as (view, pipe):
            view.scroll_transcript = Mock()
            view.scroll_top = Mock()
            view.scroll_bottom = Mock()
            run = asyncio.create_task(view.app.run_async())
            try:
                await self.settle(lambda: view.app.is_running)
                pipe.send_bytes(b"\x1b[5~")
                await self.settle(lambda: view.scroll_transcript.call_count == 1)
                view.scroll_transcript.assert_called_with(-1)
                pipe.send_bytes(b"\x1b[6~")
                await self.settle(lambda: view.scroll_transcript.call_count == 2)
                view.scroll_transcript.assert_called_with(1)
                view.app.layout.focus(view.transcript_control)
                pipe.send_bytes(b"\x1b[H")
                await self.settle(lambda: view.scroll_top.call_count == 1)
                pipe.send_bytes(b"\x1b[F")
                await self.settle(lambda: view.scroll_bottom.call_count == 1)
                view.app.layout.focus(view.input)
                view.input.text = "first line\nsecond line"
                view.input.buffer.cursor_position = 4
                pipe.send_bytes(b"\x1b[H")
                await self.settle(lambda: view.input.buffer.cursor_position == 0)
                pipe.send_bytes(b"\x1b[F")
                await self.settle(lambda: view.input.buffer.cursor_position == 10)
                view.scroll_top.assert_called_once_with()
                view.scroll_bottom.assert_called_once_with()
            finally:
                if view.app.is_running:
                    view.app.exit()
                await run
                view._reads.close()
                view._render_reads.close()
                view._operations.close()

    async def test_chat_start_schedules_provider_preparation_off_the_ui_thread(self):
        with terminal() as (view, _pipe):
            service = Mock()
            view.account_service = service
            submitted = []
            view._read_in_background = lambda read, *_args, **_kwargs: submitted.append(read)
            view.refresh_provider_catalog = Mock()
            view.refresh_sidebar = Mock()
            view.refresh_open_conversation = Mock()
            view.app.create_background_task = lambda coroutine: coroutine.close()

            def run(*, pre_run):
                pre_run()

            view.app.run = run
            with patch(
                "prompt_toolkit.patch_stdout.patch_stdout", return_value=nullcontext()
            ) as stdout_patch:
                view.run()
            stdout_patch.assert_called_once_with(raw=True)

        self.assertIn(service.prepare_enabled, submitted)
        view.refresh_provider_catalog.assert_called_once_with()
        view.refresh_sidebar.assert_called_once_with()

    async def test_settings_tab_can_switch_reopen_and_close_with_global_keys(self):
        with terminal(settings_rows=lambda: []) as (view, pipe):
            view._read_in_background = immediate
            run = asyncio.create_task(view.app.run_async())
            try:
                await self.settle(lambda: view.app.is_running)
                pipe.send_bytes(b"\x1b[15~")  # F5 settings
                await self.settle(lambda: view.tab_kind == "settings")
                settings_tab = view.tabs[view.active_tab]
                pipe.send_bytes(b"\x1b\x1b[D")  # Alt+Left
                await self.settle(lambda: view.tab_kind == "chat")
                self.assertFalse(view.settings_dialog.visible)
                pipe.send_bytes(b"\x1b[15~")
                await self.settle(lambda: view.tab_kind == "settings")
                self.assertIs(view.tabs[view.active_tab], settings_tab)
                self.assertEqual(len(view.tabs), 2)
                view.app.layout.focus(view.settings_dialog.editor)
                pipe.send_bytes(b"\x17")  # Ctrl+W closes settings even from its editor
                await self.settle(lambda: view.tab_kind == "chat")
                self.assertFalse(any(tab.get("kind") == "settings" for tab in view.tabs))
            finally:
                view.app.exit()
                await run

    async def test_f1_toggle_redraw_keeps_gutter_snapshot_consistent(self):
        with terminal(initial_messages=[{"role": "assistant", "text": "Original reply"}]) as (
            view,
            pipe,
        ):
            run = asyncio.create_task(view.app.run_async())
            try:
                await self.settle(lambda: view.app.is_running)
                for _ in range(2):
                    pipe.send_bytes(b"\x1bOP")
                    await self.settle(lambda: any(m.get("local_help") for m in view.messages))
                    await self.settle(lambda: len(getattr(view, "_transcript_roles", ())) == 2)
                    pipe.send_bytes(b"\x1bOP")
                    await self.settle(lambda: not any(m.get("local_help") for m in view.messages))
                    view.app.invalidate()
                    await asyncio.sleep(0.08)
                    self.assertEqual(view.messages[0]["text"], "Original reply")
            finally:
                view.app.exit()
                await run

    async def test_active_chat_shows_a_stop_button_that_cancels_the_current_response(self):
        with terminal() as (view, _pipe):
            connection = Mock()
            tab = view.tabs[view.active_tab]
            tab.update(busy=True, connection=connection)
            view._sync_activity()
            stop = next(
                fragment for fragment in view.render_composer_actions() if "Durdur" in fragment[1]
            )
            stop[2](
                type(
                    "MouseUp",
                    (),
                    {"event_type": MouseEventType.MOUSE_UP, "button": MouseButton.LEFT},
                )()
            )
            self.assertTrue(tab["cancel_requested"])
            self.assertEqual(view.status, view.tr("Yanıt durduruluyor…"))
            connection.shutdown.assert_called_once_with(2)
            connection.close.assert_called_once_with()

    async def test_escape_arms_stop_and_second_escape_cancels_active_response(self):
        with terminal() as (view, _pipe):
            connection = Mock()
            tab = view.tabs[view.active_tab]
            tab.update(busy=True, connection=connection)
            view._sync_activity()
            self.assertTrue(view.handle_stop_escape())
            self.assertIn("Esc'ye tekrar", view.status)
            self.assertIn("Esc'ye tekrar", "".join(f[1] for f in view.render_activity()))
            self.assertFalse(tab.get("cancel_requested", False))
            self.assertTrue(view.handle_stop_escape())
            self.assertTrue(tab["cancel_requested"])
            connection.shutdown.assert_called_once_with(2)

    async def test_double_escape_key_sequence_stops_active_response(self):
        with terminal() as (view, pipe):
            connection = Mock()
            tab = view.tabs[view.active_tab]
            tab.update(busy=True, connection=connection)
            view._sync_activity()
            run = asyncio.create_task(view.app.run_async())
            try:
                await self.settle(lambda: view.app.is_running)
                pipe.send_bytes(b"\x1b")
                await self.settle(lambda: "Esc'ye tekrar" in view.status, timeout=3)
                pipe.send_bytes(b"\x1b")
                await self.settle(lambda: tab.get("cancel_requested", False), timeout=3)
                connection.shutdown.assert_called_once_with(2)
            finally:
                view.app.exit()
                await run

    async def test_remapped_shortcuts_replace_cached_bindings_in_every_focus(self):
        with terminal() as (view, pipe):
            run = asyncio.create_task(view.app.run_async())
            try:
                await self.settle(lambda: view.app.is_running)
                # Populate the combined key-binding cache in both layout states.
                pipe.send_bytes(b"\x1b[18~")  # F7
                await self.settle(lambda: not view.sidebar_visible)
                pipe.send_bytes(b"\x1b[18~")
                await self.settle(lambda: view.sidebar_visible)
                view.apply_preferences(
                    {"keybindings": {"ui.toggle_sidebar": "c-b", "chat.new": "f11"}}
                )
                for control in (view.input.control, view.transcript_control, view.sidebar_control):
                    view.app.layout.focus(control)
                    view.app.invalidate()
                    await asyncio.sleep(0.03)
                    pipe.send_bytes(b"\x1b[18~")
                    await asyncio.sleep(0.04)
                    self.assertTrue(view.sidebar_visible)
                    pipe.send_bytes(b"\x02")  # Ctrl+B
                    await self.settle(lambda: not view.sidebar_visible)
                    pipe.send_bytes(b"\x02")
                    await self.settle(lambda: view.sidebar_visible)
                count = len(view.tabs)
                view.app.layout.focus(view.input)
                pipe.send_bytes(b"\x14")  # old Ctrl+T
                await asyncio.sleep(0.04)
                self.assertEqual(len(view.tabs), count)
                pipe.send_bytes(b"\x1b[23~")  # new F11
                await self.settle(lambda: len(view.tabs) == count + 1)
            finally:
                view.app.exit()
                await run

    async def test_registered_extension_action_uses_its_settings_shortcut(self):
        invoke = Mock(return_value=None)

        def actions():
            return [{"id": "extension.export", "keybinding": "", "available": lambda: True}]

        with terminal(
            list_actions=lambda: [{"id": "extension.export", "title": "Export"}],
            list_shortcut_actions=actions,
            invoke_action=invoke,
        ) as (view, pipe):
            run = asyncio.create_task(view.app.run_async())
            try:
                await self.settle(lambda: view.app.is_running)
                view.apply_preferences({"keybindings": {"extension.export": "c-z"}})
                pipe.send_bytes(b"\x1a")  # Ctrl+Z
                await self.settle(lambda: invoke.called and not view.background_jobs)
                invoke.assert_called_once_with("extension.export", {})
            finally:
                view.app.exit()
                await run

    async def test_search_editing_backspace_delete_space_home_end_and_ctrl_d(self):
        with terminal() as (view, pipe):
            run = asyncio.create_task(view.app.run_async())
            try:
                await self.settle(lambda: view.app.is_running)
                pipe.send_bytes(b"\x10")  # Ctrl+P
                await self.settle(lambda: view.palette_visible)
                pipe.send_text("alpha beta")
                await self.settle(lambda: view.palette_search.text == "alpha beta")
                pipe.send_bytes(b"\x08")  # Backspace on terminals emitting Ctrl+H
                await self.settle(lambda: view.palette_search.text == "alpha bet")
                pipe.send_bytes(b"\x7f")  # Backspace on terminals emitting DEL
                await self.settle(lambda: view.palette_search.text == "alpha be")
                pipe.send_bytes(b"\x1b[H\x1b[3~")  # Home + Delete
                await self.settle(lambda: view.palette_search.text == "lpha be")
                pipe.send_bytes(b"\x04")  # Ctrl+D must delete in the search buffer, not quit
                await self.settle(lambda: view.palette_search.text == "pha be")
                pipe.send_bytes(b"\x1b[F")  # End belongs to the search field
                await self.settle(
                    lambda: view.palette_search.buffer.cursor_position == len("pha be")
                )
                self.assertTrue(view.app.is_running)
            finally:
                if view.app.is_running:
                    view.app.exit()
                await asyncio.wait_for(run, 2)

    async def test_sidebar_and_composer_text_fields_do_not_lose_spaces_or_backspaces(self):
        with terminal(keybindings={"chat.reload": "c-h"}) as (view, pipe):
            run = asyncio.create_task(view.app.run_async())
            try:
                await self.settle(lambda: view.app.is_running)
                pipe.send_text("hello world")
                await self.settle(lambda: view.input.text == "hello world")
                pipe.send_bytes(b"\x08")
                await self.settle(lambda: view.input.text == "hello worl")
                # Focus the existing search widget without causing network activity.
                view.app.layout.focus(view.sidebar_search)
                pipe.send_text("project notes")
                await self.settle(lambda: view.sidebar_search.text == "project notes")
                pipe.send_bytes(b"\x08")
                await self.settle(lambda: view.sidebar_search.text == "project note")
                self.assertEqual(view.input.text, "hello worl")
            finally:
                if view.app.is_running:
                    view.app.exit()
                await asyncio.wait_for(run, 2)

    async def test_long_sidebar_and_palette_keep_keyboard_selection_in_view(self):
        with terminal() as (view, pipe):
            view.sidebar_items = [{"id": str(i), "title": f"Conversation {i}"} for i in range(80)]
            view._build_sidebar_rows()
            run = asyncio.create_task(view.app.run_async())
            try:
                await self.settle(lambda: view.app.is_running)
                view.focus_sidebar()
                pipe.send_bytes(b"\x1b[B" * 60)
                await self.settle(lambda: view.sidebar_index == 60)
                await self.settle(
                    lambda: (
                        view.sidebar_window.render_info is not None
                        and view.sidebar_window.render_info.first_visible_line() > 0
                    )
                )
                self.assertIn(view.sidebar_index, view.sidebar_window.render_info.displayed_lines)
                view.list_actions = lambda: [
                    {"id": f"test.action.{i}", "title": f"Action {i}"} for i in range(80)
                ]
                view.open_palette()
                pipe.send_bytes(b"\x1b[B" * 60)
                await self.settle(lambda: view.palette_index == 60)
                await self.settle(
                    lambda: (
                        view.palette_window.render_info is not None
                        and view.palette_window.render_info.first_visible_line() > 0
                    )
                )
                self.assertIn(view.palette_index, view.palette_window.render_info.displayed_lines)
            finally:
                if view.app.is_running:
                    view.app.exit()
                await asyncio.wait_for(run, 2)

    async def test_navigation_shortcuts_close_overlays_before_changing_focus(self):
        with terminal() as (view, pipe):
            view._read_in_background = immediate
            run = asyncio.create_task(view.app.run_async())
            try:
                await self.settle(lambda: view.app.is_running)
                view.open_palette()
                pipe.send_bytes(b"\x06")  # Ctrl+F
                await self.settle(
                    lambda: view.app.layout.current_control is view.sidebar_search.control
                )
                self.assertFalse(view.palette_visible)
                view.open_palette()
                pipe.send_bytes(b"\x07")  # Ctrl+G
                await self.settle(lambda: view.app.layout.current_control is view.sidebar_control)
                self.assertFalse(view.palette_visible)
                view.open_palette()
                pipe.send_bytes(b"\x14")  # Ctrl+T
                await self.settle(lambda: len(view.tabs) == 2)
                self.assertFalse(view.palette_visible)
                self.assertIs(view.app.layout.current_control, view.input.control)
            finally:
                if view.app.is_running:
                    view.app.exit()
                await asyncio.wait_for(run, 2)

    async def test_resize_recomputes_sidebar_transcript_and_tab_widths(self):
        from prompt_toolkit.utils import get_cwidth

        from tui.styles import DEFAULT_TRANSCRIPT_GUTTER_WIDTH

        class Output(DummyOutput):
            size = Size(rows=24, columns=80)

            def get_size(self):
                return self.size

        output = Output()
        with terminal(output=output) as (view, _pipe):
            view.messages = [{"role": "assistant", "text": "A readable response. " * 20}]
            view.title = "日本語 🚀 Çok uzun bir sohbet başlığı"
            run = asyncio.create_task(view.app.run_async())
            try:
                await self.settle(lambda: view.app.is_running)
                for columns, rows in ((80, 24), (60, 18), (120, 40)):
                    output.size = Size(rows=rows, columns=columns)
                    view.app.invalidate()
                    expected = columns - view.sidebar_width() - 1
                    await self.settle(
                        lambda: (
                            view.transcript_window.render_info is not None
                            and view.transcript_window.render_info.window_width == expected - 1
                        )
                    )
                    self.assertEqual(
                        view.transcript_width(),
                        min(100, expected - DEFAULT_TRANSCRIPT_GUTTER_WIDTH - 1),
                    )
                    self.assertLessEqual(
                        sum(get_cwidth(fragment[1]) for fragment in view.render_tabs()), columns
                    )
                view.toggle_sidebar()
                await self.settle(
                    lambda: (
                        view.transcript_window.render_info is not None
                        and view.transcript_window.render_info.window_width == 120 - 1
                    )
                )
                self.assertEqual(view.transcript_width(), 100)
            finally:
                if view.app.is_running:
                    view.app.exit()
                await asyncio.wait_for(run, 2)

    async def test_sidebar_search_accepts_mouse_focus_and_divider_stays_aligned(self):
        class Output(DummyOutput):
            size = Size(rows=24, columns=80)

            def get_size(self):
                return self.size

        with terminal(output=Output()) as (view, _pipe):
            service = Mock()
            service.items.return_value = []
            service.active_accounts.return_value = []
            service.api.providers.get.return_value.capabilities = frozenset()
            view.account_service = service
            view.archive_service = lambda: None
            view.sync_states = lambda _archive: ()
            view.downloads_panel.service = lambda: None
            run = asyncio.create_task(view.app.run_async())
            try:
                await self.settle(lambda: view.app.is_running)
                await self.settle(
                    lambda: (
                        view.app.renderer._last_screen is not None
                        and any(
                            "Ara" in line and "⌕" in line and "≡" in line
                            for line in screen_text(view).splitlines()
                        )
                    )
                )

                self.assertTrue(view.sidebar_search.control.focus_on_click())
                screen = view.app.renderer._last_screen
                search_row = next(
                    row
                    for row in screen.data_buffer.values()
                    if "Ara" in "".join(cell.char for _, cell in sorted(row.items()))
                )
                divider = search_row.get(view.sidebar_width())
                self.assertIsNotNone(divider)
                self.assertEqual(divider.char, "│")
            finally:
                if view.app.is_running:
                    view.app.exit()
                await asyncio.wait_for(run, 2)

"""Render shared forms and tab states through the running terminal application."""

import asyncio
import unittest
from unittest.mock import Mock

from prompt_toolkit.styles import Style

from providers import Capability
from tests import test_cli_interaction as interaction
from tests.test_account_ui import Accounts
from tests.test_cli_interaction import terminal
from tests.test_terminal_responsiveness import ResizableOutput
from tests.test_ui_controls import immediate


def screen_text(view):
    screen = view.app.renderer._last_screen
    return "\n".join(
        "".join(cell.char for _, cell in sorted(row.items()))
        for _, row in sorted(screen.data_buffer.items())
    )


class FormsRenderTests(unittest.IsolatedAsyncioTestCase):
    settle = interaction.InputTests.settle

    async def test_filter_and_project_account_choices_are_painted_and_resize(self):
        service = Accounts()
        service.active_accounts = lambda: service.items()
        service.capabilities = lambda _: frozenset({Capability.PROJECT_CREATE})
        output = ResizableOutput()
        with terminal(output=output, account_service=service) as (v, _):
            run = asyncio.create_task(v.app.run_async())
            try:
                await self.settle(lambda: v.app.is_running)
                for columns, rows in [(100, 30), (80, 24), (60, 18)]:
                    output.columns, output.rows = columns, rows
                    v.open_filters()
                    v.app.invalidate()
                    await asyncio.sleep(0.06)
                    self.assertIn("ChatGPT", screen_text(v))
                    self.assertIn("Gemini", screen_text(v))
                    self.assertNotIn("Window too small", screen_text(v))
                    v.forms.close()
                    v.open_project_create()
                    await asyncio.sleep(0.06)
                    self.assertIn("ChatGPT", screen_text(v))
                    self.assertIn("Gemini", screen_text(v))
                    v.forms.close()
            finally:
                v.app.exit()
                await run

    async def test_checkbox_selection_is_saved_as_complete_account_ids(self):
        service = Accounts()
        with terminal(account_service=service) as (v, pipe):
            v.refresh_sidebar = Mock()
            run = asyncio.create_task(v.app.run_async())
            try:
                await self.settle(lambda: v.app.is_running)
                v.open_filters()
                v.forms.fields["accounts"].current_values = ["gemini/personal"]
                pipe.send_bytes(b"\x13")
                await self.settle(lambda: not v.forms.visible)
                self.assertEqual(v.account_filter, {"gemini/personal"})
                self.assertEqual(v.provider_filter, {"gemini"})
            finally:
                v.app.exit()
                await run

    async def test_deletion_confirmation_is_visible_and_executes_the_remote_action(self):
        service = Accounts()
        service.capabilities = lambda _: frozenset({Capability.DELETE})
        value = "@chatgpt/default/disposable"
        service.delete = Mock(return_value={"removed": [value]})
        with terminal(account_service=service) as (v, pipe):
            v._read_in_background = immediate
            run = asyncio.create_task(v.app.run_async())
            try:
                await self.settle(lambda: v.app.is_running)
                v.delete_record(value=value)
                await asyncio.sleep(0.06)
                self.assertIn("Kalıcı olarak sil", screen_text(v))
                service.delete.assert_not_called()
                v.app.layout.focus(v.forms.confirm_buttons[0])
                pipe.send_bytes(b"\r")
                await self.settle(lambda: service.delete.called)
                service.delete.assert_called_once_with(value, "chatgpt/default", "conversation")
            finally:
                v.app.exit()
                await run

    def test_active_tab_style_wins_over_the_open_tab_color(self):
        with terminal() as (v, _):
            v.new_tab()
            fragments = v.render_tabs()
            labels = [(style, text) for style, text, *_ in fragments if "class:tab.open" in style]
            self.assertEqual(len(labels), 2)
            active = next(style for style, _ in labels if "class:tab.active" in style)
            self.assertLess(active.index("class:tab.open"), active.index("class:tab.active"))
            style = Style.from_dict({"tab.open": "bold #ffffff", "tab.active": "#a9cfff"})
            self.assertEqual(style.get_attrs_for_style_str(active).color, "a9cfff")

    def test_split_group_label_keeps_the_active_color(self):
        from extensions.layout.panes import SplitLayout

        with terminal(layout_factory=SplitLayout) as (v, _):
            v.title = "Left"
            v.new_tab()
            v.title = "Right"
            v.layout_controller.begin()
            v.select_tab(0)
            v.layout_controller.attach()
            labels = [
                (style, text) for style, text, *_ in v.render_tabs() if "class:tab.open" in style
            ]
            self.assertEqual(len(labels), 1)
            self.assertIn("class:tab.active", labels[0][0])
            self.assertIn("◫", labels[0][1])

    async def test_inline_project_editor_uses_available_width_on_resize(self):
        output = ResizableOutput()
        with terminal(output=output) as (v, _):
            run = asyncio.create_task(v.app.run_async())
            try:
                await self.settle(lambda: v.app.is_running)
                v.tab_kind = "project"
                v.current_project = {"id": "p"}
                v.forms.open(
                    "Projeyi düzenle",
                    [
                        ("name", "Proje adı", "Testo", None),
                        ("instructions", "Talimatlar", "", None),
                    ],
                    lambda _: None,
                    inline_project="p",
                )
                for width, height in [(100, 30), (80, 24), (60, 18)]:
                    output.columns, output.rows = width, height
                    v.app.invalidate()
                    await asyncio.sleep(0.08)
                    self.assertIn("Kaydet", screen_text(v))
                    self.assertNotIn("Window too small", screen_text(v))
                    field = v.forms.fields["name"].window
                    self.assertGreaterEqual(field.render_info.window_width, v.main_width() - 2)
            finally:
                v.app.exit()
                await run

    async def test_confirmation_defaults_to_no_and_escape_cancels(self):
        with terminal() as (v, pipe):
            callback = Mock()
            run = asyncio.create_task(v.app.run_async())
            try:
                await self.settle(lambda: v.app.is_running)
                v.forms.confirm("Sohbeti sil", callback)
                self.assertIs(v.app.layout.current_control, v.forms.confirm_buttons[1].control)
                pipe.send_bytes(b"\r")
                await self.settle(lambda: not v.forms.visible)
                callback.assert_not_called()
                v.forms.confirm("Projeyi sil", callback)
                pipe.send_bytes(b"\x1b")
                await self.settle(lambda: not v.forms.visible)
                callback.assert_not_called()
            finally:
                v.app.exit()
                await run

    def test_project_delete_picker_includes_all_providers(self):
        with terminal() as (v, _):
            v.projects_loaded = True
            v.project_items = {
                "@chatgpt/default/p": {"name": "GPT"},
                "@gemini/default/p": {"name": "Gemini"},
            }
            v.handle_command("/project-delete")
            self.assertEqual({i["title"] for i in v.palette_items}, {"GPT", "Gemini"})
            v.delete_record = Mock()
            v.palette_filtered = v.palette_items
            v.palette_index = 1
            v.execute_palette_item()
            v.delete_record.assert_called_once_with("container", "@gemini/default/p")

    def test_close_all_tabs_clears_groups_and_keeps_a_fresh_draft(self):
        from extensions.layout.panes import SplitLayout

        with terminal(layout_factory=SplitLayout) as (v, _):
            v.new_tab()
            v.layout_controller.begin()
            v.select_tab(0)
            v.layout_controller.attach()
            v.handle_command("/tabs-close")
            self.assertEqual(len(v.tabs), 1)
            self.assertIsNone(v.conversation_id)
            self.assertFalse(v.layout_controller.groups)
            self.assertEqual(v.input.text, "")
            self.assertEqual(v.render_tabs()[-1][1], " ⊗ ")

    def test_close_all_preserves_tabs_with_running_work(self):
        with terminal() as (v, _):
            v.new_tab()
            v.tabs[0]["busy"] = True
            v.close_all_tabs()
            self.assertEqual(len(v.tabs), 2)

    def test_delete_buttons_use_the_visible_record(self):
        with terminal() as (v, _):
            v.conversation_id = "@chatgpt/default/chat"
            v.delete_record = Mock()
            from prompt_toolkit.data_structures import Point
            from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

            click = MouseEvent(Point(0, 0), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset())
            controls = v.render_composer_actions()
            self.assertEqual(controls[1][1], " Sil ")
            controls[1][2](click)
            v.delete_record.assert_called_once_with(value=v.conversation_id)
            v.delete_record.reset_mock()
            v.tab_kind = "project"
            v.current_project = {"id": "@gemini/default/notebook"}
            delete = next(fragment for fragment in v.render_chat_header() if fragment[1] == " Sil ")
            delete[2](click)
            v.delete_record.assert_called_once_with("container", v.current_project["id"])

    def test_close_all_cleans_temporary_records_in_their_account(self):
        service = Accounts()
        cleanup = Mock()
        service.callbacks = Mock(return_value={"forget_temporary": cleanup})
        with terminal(account_service=service) as (v, _):
            v._read_in_background = immediate
            v.new_tab()
            v.tabs[0].update(
                temporary=True,
                conversation_id="@gemini/personal/temp",
                account_key="gemini/personal",
            )
            v.close_all_tabs()
            service.callbacks.assert_any_call("gemini/personal")
            cleanup.assert_called_once_with("@gemini/personal/temp")
            self.assertEqual(len(v.tabs), 1)

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
                v.forms.fields["confirm"].current_value = "delete"
                pipe.send_bytes(b"\x13")
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

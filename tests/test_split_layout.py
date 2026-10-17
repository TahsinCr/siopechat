"""Real split layout composition with isolated terminal input."""

import asyncio
import unittest
from unittest.mock import patch

from extensions.layout.panes import SplitLayout, _Pane
from tests import test_cli_interaction as interaction


class SplitTests(unittest.IsolatedAsyncioTestCase):
    settle = interaction.InputTests.settle

    async def test_two_panes_render_focus_and_extension_disable(self):
        with interaction.terminal(layout_factory=SplitLayout) as (v, _):
            v.title = "Left"
            v.messages = [{"role": "assistant", "text": "Left body"}]
            v.new_tab()
            v.title = "Right"
            v.messages = [{"role": "user", "text": "Right body"}]
            v.layout_controller.begin()
            self.assertIsNotNone(v.layout_controller.pending)
            v.select_tab(0)
            v.layout_controller.attach()
            run = asyncio.create_task(v.app.run_async())
            try:
                await self.settle(lambda: v.transcript_window.render_info is not None)
                self.assertEqual(v.layout_controller.columns(), 2)
                pane = next(iter(v.layout_controller._panes.values()))
                pane.render()
                self.assertTrue(pane.message_ranges)
                self.assertEqual(pane.body.left_margins[0].get_width(None), 3)
                self.assertLess(v.main_width(), v.workspace_width())
                v.select_tab(1)
                v.app.invalidate()
                await asyncio.sleep(0.08)
                self.assertEqual(v.title, "Right")
                v.layout_enabled = False
                v.app.invalidate()
                await asyncio.sleep(0.08)
                self.assertEqual(v.main_width(), v.workspace_width())
                v.layout_enabled = True
                v.layout_controller.unsplit()
                self.assertEqual(v.layout_controller.columns(), 1)
            finally:
                v.app.exit()
                await run

    async def test_tab_switch_preserves_manual_reading_position(self):
        with interaction.terminal() as (v, _):
            v.follow_output = False
            v.transcript_window.vertical_scroll = 12
            v.new_tab()
            self.assertTrue(v.follow_output)
            v.switch_tab(-1)
            self.assertFalse(v.follow_output)
            self.assertEqual(v.transcript_window.vertical_scroll, 12)

    async def test_inactive_busy_pane_keeps_old_message_render_cache(self):
        with interaction.terminal(layout_factory=SplitLayout) as (v, _):
            v.tabs[0]["messages"] = [
                {"role": "user", "text": "Old"},
                {"role": "assistant", "text": "New"},
            ]
            v.tabs[0]["busy"] = True
            pane = _Pane(v, v.tabs[0]["tab_id"])
            with patch.object(pane.renderer, "render", wraps=pane.renderer.render) as render:
                pane.render()
                self.assertFalse(render.call_args_list[0].args[-1])
                self.assertTrue(render.call_args_list[-1].args[-1])
                pane.render()
                self.assertEqual(render.call_count, 2)

    async def test_pending_split_can_cancel_and_cannot_duplicate_same_tab(self):
        with interaction.terminal(layout_factory=SplitLayout) as (v, _):
            v.begin_split("rows")
            v.layout_controller.attach()
            self.assertIsNotNone(v.layout_controller.pending)
            self.assertFalse(v.layout_controller.groups)
            v.layout_controller.unsplit()
            self.assertIsNone(v.layout_controller.pending)

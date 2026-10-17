"""Real split layout composition with isolated terminal input."""

import asyncio
import unittest

from extensions.layout.panes import SplitLayout
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

    async def test_pending_split_can_cancel_and_cannot_duplicate_same_tab(self):
        with interaction.terminal(layout_factory=SplitLayout) as (v, _):
            v.begin_split("rows")
            v.layout_controller.attach()
            self.assertIsNotNone(v.layout_controller.pending)
            self.assertFalse(v.layout_controller.groups)
            v.layout_controller.unsplit()
            self.assertIsNone(v.layout_controller.pending)

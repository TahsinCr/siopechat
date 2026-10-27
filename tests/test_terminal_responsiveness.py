"""Resize and SDK output regressions without account requests."""

import asyncio
import unittest

from prompt_toolkit.data_structures import Point
from prompt_toolkit.layout.controls import FormattedTextControl

from tests import tui_support as interaction
from tests.tui_support import ResizableOutput, immediate
from tui.widgets import BoundedTextControl


class CursorRenderingTests(unittest.TestCase):
    def test_wrapped_cursor_is_clamped_to_the_current_snapshot(self):
        content = "short"
        control = FormattedTextControl(lambda: content, get_cursor_position=lambda: Point(0, 599))
        previous = control.create_content(8, 20)
        with self.assertRaises(IndexError):
            previous.get_height_for_line(previous.cursor_position.y, 8, None)

        bounded = BoundedTextControl(lambda: content, get_cursor_position=lambda: Point(99, 599))
        current = bounded.create_content(8, 20)
        self.assertEqual(current.cursor_position, Point(5, 0))
        self.assertEqual(current.get_height_for_line(current.cursor_position.y, 8, None), 1)


class ResponsiveSettingsTests(unittest.IsolatedAsyncioTestCase):
    settle = staticmethod(interaction.wait_until)

    async def test_settings_resize_and_scroll_to_focused_controls(self):
        output = ResizableOutput()
        rows = [
            {
                "section": "keybindings",
                "key": f"command.{i}",
                "title": f"Command {i}",
                "value": "c-a",
            }
            for i in range(60)
        ]
        with interaction.terminal(
            output=output, settings_rows=lambda: rows, session_action=lambda _: {}
        ) as (
            view,
            _,
        ):
            view._read_in_background = immediate
            run = asyncio.create_task(view.app.run_async())
            try:
                await self.settle(lambda: view.app.is_running)
                view.settings_dialog.open("keybindings")
                for columns, height in [(120, 34), (80, 24), (60, 18), (40, 12), (120, 34)]:
                    output.columns, output.rows = columns, height
                    view.app.invalidate()
                    await asyncio.sleep(0.06)
                    self.assertEqual(view.settings_dialog.compact(), columns < 76)
                    self.assertEqual(
                        view.settings_dialog.content_width(),
                        columns - (0 if columns < 76 else 20) - 1,
                    )
                    view.app.layout.focus(view.settings_dialog.save_button)
                    view.app.invalidate()
                    await asyncio.sleep(0.06)
                    screen = view.app.renderer._last_screen
                    text = "\n".join(
                        "".join(screen.data_buffer[y][x].char for x in range(columns))
                        for y in range(height)
                    )
                    self.assertIn("Kaydet", text)
                    self.assertNotIn("Window too small", text)
                view.settings_dialog.choose_category("accounts")
                output.columns, output.rows = 40, 12
                view.app.layout.focus(view.session_dialog.close_button)
                view.app.invalidate()
                await asyncio.sleep(0.08)
                screen = view.app.renderer._last_screen
                text = "\n".join(
                    "".join(screen.data_buffer[y][x].char for x in range(40)) for y in range(12)
                )
                self.assertIn("Kapat", text)
                self.assertEqual(
                    view.app.layout.current_control, view.settings_dialog.cancel_button.control
                )
                self.assertIn("Kapat", text.splitlines()[-2])
            finally:
                view.app.exit()
                await run


class BackgroundCapacityTests(unittest.TestCase):
    def test_metadata_queue_is_bounded_and_shutdown_rejects_new_work(self):
        import threading

        from tui.background import BackgroundReads

        pool = BackgroundReads(workers=1, capacity=2)
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        queued = []

        def work():
            started.set()
            release.wait(3)
            finished.set()

        try:
            self.assertTrue(pool.submit(work))
            self.assertTrue(started.wait(1))
            self.assertTrue(pool.submit(lambda: queued.append(True)))
            self.assertFalse(pool.submit(lambda: None))
            pool.close()
            self.assertFalse(pool.submit(lambda: None))
        finally:
            release.set()
            self.assertTrue(finished.wait(2))
            pool.close()
        self.assertEqual(queued, [])

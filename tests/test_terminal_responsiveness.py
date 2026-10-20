"""Resize and SDK output regressions without account requests."""

import asyncio
import unittest

from prompt_toolkit.data_structures import Size
from prompt_toolkit.output import DummyOutput

from tests import test_cli_interaction as interaction
from tests.test_ui_controls import immediate


class ResizableOutput(DummyOutput):
    columns, rows = 120, 34

    def get_size(self):
        return Size(rows=self.rows, columns=self.columns)


class ResponsiveSettingsTests(unittest.IsolatedAsyncioTestCase):
    settle = interaction.InputTests.settle

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

        from cli.background import BackgroundReads

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

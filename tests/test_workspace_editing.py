"""Composer and terminal text-selection behavior."""

import asyncio
import unittest
from unittest.mock import patch

from providers import ProviderRegistry, ReplaceText, SendRequest, TextDelta
from tasks import TaskService
from tests.tui_support import immediate, terminal, wait_until


class EditingTests(unittest.IsolatedAsyncioTestCase):
    settle = staticmethod(wait_until)

    async def test_composer_grows_scrolls_selects_copies_and_pastes(self):
        with (
            terminal() as (v, p),
            patch("utils.clipboard.copy_text", return_value=True) as copied,
            patch("utils.clipboard.read_text", return_value="Pasted"),
        ):
            run = asyncio.create_task(v.app.run_async())
            try:
                await self.settle(lambda: v.app.is_running)
                v.input.text = "\n".join("line" for _ in range(12))
                v.input.buffer.cursor_position = len(v.input.text)
                await self.settle(
                    lambda: (
                        v.input.window.render_info is not None
                        and v.input.window.render_info.window_height == 6
                    )
                )
                self.assertGreater(v.input.window.render_info.vertical_scroll, 0)
                v.input.text = "Select all"
                p.send_bytes(b"\x01\x03")
                await self.settle(lambda: copied.called)
                copied.assert_called_with("Select all")
                p.send_bytes(b"\x01\x16")
                await self.settle(lambda: v.input.text == "Pasted")
            finally:
                if v.app.is_running:
                    v.app.exit()
                await run

    async def test_settings_editor_supports_select_all_and_copy(self):
        with (
            terminal(
                settings_rows=lambda: [
                    {"section": "application", "key": "model", "value": "abc", "title": "Model"}
                ]
            ) as (v, p),
            patch("utils.clipboard.copy_text", return_value=True) as copied,
        ):
            v._read_in_background = immediate
            run = asyncio.create_task(v.app.run_async())
            try:
                await self.settle(lambda: v.app.is_running)
                v.settings_dialog.open()
                v.settings_dialog.focus_editor()
                p.send_bytes(b"\x01\x03")
                await self.settle(lambda: copied.called)
                copied.assert_called_with("abc")
            finally:
                if v.app.is_running:
                    v.app.exit()
                await run

    async def test_task_snapshot_keeps_reasoning_out_of_answer(self):
        class Provider:
            id = "demo"
            capabilities = frozenset()

            async def stream(self, request):
                yield TextDelta("Summary", channel="analysis")
                yield ReplaceText("Updated", channel="analysis")
                yield TextDelta("Answer")

        from providers import Capability

        Provider.capabilities = frozenset([Capability.CHAT])
        registry = ProviderRegistry()
        registry.register(Provider())
        service = TaskService(registry)
        try:
            result = await service.wait(
                service.submit(SendRequest("demo", "default", "chat", "Question"))
            )
            self.assertEqual(result.text, "Answer")
            self.assertEqual(result.reasoning, "Updated")
        finally:
            await service.close()

    async def test_message_click_select_all_and_copy(self):
        with (
            terminal(
                initial_messages=[
                    {"role": "user", "text": "Question"},
                    {"role": "assistant", "text": "Answer"},
                ]
            ) as (v, p),
            patch("utils.clipboard.copy_text", return_value=True) as copied,
        ):
            run = asyncio.create_task(v.app.run_async())
            try:
                await self.settle(lambda: v.app.is_running)
                v.select_message(1)
                p.send_bytes(b"\x01\x03")
                await self.settle(lambda: copied.called)
                copied.assert_called_with("Answer")
                self.assertTrue(
                    any(
                        "class:message.selected" in f[0]
                        for number in range(v.transcript_control.create_content(80, 24).line_count)
                        for f in v.transcript_control.create_content(80, 24).get_line(number)
                    )
                )
            finally:
                if v.app.is_running:
                    v.app.exit()
                await run

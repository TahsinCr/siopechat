"""Actual extended-key input and terminal mode lifecycle."""

import asyncio
import unittest
from unittest.mock import Mock

from prompt_toolkit.output import DummyOutput

from tests import cli_support as interaction


class RecordingOutput(DummyOutput):
    def __init__(self):
        self.events = []

    def write_raw(self, text):
        self.events.append(text)

    def quit_alternate_screen(self):
        self.events.append("quit-alternate-screen")


class KeyboardTests(unittest.IsolatedAsyncioTestCase):
    settle = staticmethod(interaction.wait_until)

    async def test_modified_enter_inserts_newline_plain_enter_sends_and_ctrl_d_survives(self):
        output = RecordingOutput()
        with interaction.terminal(output=output) as (view, pipe):
            view.submit_prompt = Mock()
            view.request_quit = Mock()
            run = asyncio.create_task(view.app.run_async())
            try:
                await self.settle(lambda: view.app.is_running)
                pipe.send_text("one")
                for index, sequence in enumerate((b"\x1b[13;2u", b"\x1b[27;2;13~", b"\x1b[13;2~")):
                    pipe.send_bytes(sequence)
                    await self.settle(lambda: view.input.text == "one" + "\n" * (index + 1))
                self.assertFalse(view.submit_prompt.called)
                pipe.send_text("two")
                pipe.send_bytes(b"\x1b[13u")
                await self.settle(lambda: view.submit_prompt.call_count == 1)
                pipe.send_bytes(b"\x1b[100;5u")
                await self.settle(lambda: view.request_quit.called)
                self.assertIn("\x1b[>4;2m\x1b[>1u", output.events)
                view.forms.open(
                    "Edit", [("instructions", "Instructions", "a", None)], lambda _: None
                )
                view.app.layout.focus(view.forms.fields["instructions"])
                view.forms.fields["instructions"].buffer.cursor_position = 1
                pipe.send_bytes(b"\x1b[13;2u")
                await self.settle(lambda: view.forms.fields["instructions"].text == "a\n")
                self.assertTrue(view.forms.visible)
                pipe.send_bytes(b"\x1b[13u")
                await self.settle(lambda: view.forms.fields["instructions"].text == "a\n\n")

            finally:
                view.app.exit()
                await run
            stop = output.events.index("\x1b[<u\x1b[>4;0m")
            self.assertIn("quit-alternate-screen", output.events[stop + 1 :])

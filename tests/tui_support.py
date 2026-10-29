"""Shared CLI test helpers, kept separate from test case modules."""

import asyncio
from contextlib import contextmanager
from unittest.mock import patch

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point, Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output import DummyOutput

from tui.chat import ChatView


@contextmanager
def terminal(output=None, **callbacks):
    with (
        create_pipe_input() as pipe,
        patch(
            "tui.chat.view.Application",
            side_effect=lambda **kw: Application(
                input=pipe, output=output if output is not None else DummyOutput(), **kw
            ),
        ),
    ):
        view = ChatView(lambda: [], lambda _: {}, lambda *_: {}, **callbacks)
        try:
            yield view, pipe
        finally:
            view.close_background_work(wait=True)


def immediate(read, success, failure, **kwargs):
    try:
        success(read())
    except Exception as error:
        failure(str(error))


def click(fragment):
    return fragment[2](
        MouseEvent(Point(0, 0), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset())
    )


class ResizableOutput(DummyOutput):
    columns, rows = 120, 34

    def get_size(self):
        return Size(rows=self.rows, columns=self.columns)


def screen_text(view):
    screen = view.app.renderer._last_screen
    return "\n".join(
        "".join(cell.char for _, cell in sorted(row.items()))
        for _, row in sorted(screen.data_buffer.items())
    )


async def wait_until(predicate, timeout=1):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout)

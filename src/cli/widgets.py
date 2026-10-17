"""Reusable input, completion and transcript widgets."""

from __future__ import annotations

from typing import Any, Callable

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.layout import Window
from prompt_toolkit.layout.margins import Margin
from prompt_toolkit.layout.processors import Processor, Transformation
from prompt_toolkit.mouse_events import MouseButton, MouseEventType
from prompt_toolkit.utils import get_cwidth


def fit_text(text: str, width: int) -> str:
    """Fit a single UI row by terminal cells, including wide Unicode names."""
    text = " ".join(text.splitlines()).replace("\t", " ")
    text = "".join(char for char in text if ord(char) >= 32 and ord(char) != 127)
    if width <= 0:
        return ""
    if get_cwidth(text) <= width:
        return text
    used = 0
    result = []
    for char in text:
        used += get_cwidth(char)
        if used > width - 1:
            break
        result.append(char)
    return "".join(result) + "…"


def on_click(callback):
    """Use the same action callback for text buttons and keyboard commands."""

    def handle(event):
        if event.event_type == MouseEventType.MOUSE_UP and event.button == MouseButton.LEFT:
            callback()
            return None
        return NotImplemented

    return handle


class SlashCommandCompleter(Completer):
    COMMANDS = (
        ("/new", "start a new conversation"),
        ("/tab-next", "switch to the next tab"),
        ("/tab-prev", "switch to the previous tab"),
        ("/tab-close", "close the current tab"),
        ("/search", "search saved conversations"),
        ("/projects", "show provider project support"),
        ("/temporary", "new temporary conversation without local recording"),
        ("/private", "new temporary conversation"),
        ("/project", "move this chat or choose its project; /project info shows details"),
        ("/proje", "sohbeti projeye taşı veya projeden çıkar"),
        ("/refresh", "refresh models, efforts, projects and chats"),
        ("/model", "select a model ID for this session"),
        ("/effort", "show the provider effort value"),
        ("/actions", "search actions and commands"),
        ("/settings", "edit application and extension settings"),
        ("/session", "account and session controls"),
        ("/login", "open login controls"),
        ("/logout", "open local logout confirmation"),
        ("/action", "run an application action by ID"),
        ("/resume", "search recent conversations"),
        ("/history", "reload this conversation"),
        ("/rename", "rename this conversation"),
        ("/copy", "copy the latest response"),
        ("/remove", "remove the latest image"),
        ("/clear", "clear the displayed transcript"),
        ("/help", "show available commands"),
        ("/exit", "quit SiopeChat"),
        ("/quit", "quit SiopeChat"),
    )

    def get_completions(self, document: Any, complete_event: Any) -> Any:
        token = document.text_before_cursor
        if not token.startswith("/") or any(char.isspace() for char in token):
            return
        query = token.casefold()
        for command, description in self.COMMANDS:
            if command.startswith(query) and command != query:
                yield Completion(
                    command,
                    start_position=-len(token),
                    display=command,
                    display_meta=description,
                )


class ComposerPlaceholderProcessor(Processor):
    def apply_transformation(self, transformation_input: Any) -> Transformation:
        fragments = transformation_input.fragments
        if transformation_input.document.text or transformation_input.lineno != 0:
            return Transformation(fragments)
        return Transformation(
            fragments + [("class:placeholder", "Mesaj yazın…")],
            source_to_display=lambda position: position,
            display_to_source=lambda position: 0,
        )


class TranscriptWindow(Window):
    """A Window with the multi-line wheel movement users expect in terminals."""

    def __init__(self, *args: Any, on_manual_scroll: Callable[[], None], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.on_manual_scroll = on_manual_scroll

    def _scroll_up(self) -> None:
        info = self.render_info
        if info is None:
            return
        self.on_manual_scroll()
        self.vertical_scroll = max(0, self.vertical_scroll - 4)

    def _scroll_down(self) -> None:
        info = self.render_info
        if info is None:
            return
        self.on_manual_scroll()
        last_scroll = max(0, info.content_height - info.window_height)
        self.vertical_scroll = min(last_scroll, self.vertical_scroll + 4)


class TranscriptGutter(Margin):
    """Role markers follow the first display row of each rendered message."""

    def __init__(self, view):
        self.view = view

    def get_width(self, get_ui_content):
        return 3

    def create_margin(self, window_render_info, width, height):
        starts = {
            start: self.view.messages[index].get("role")
            for start, _, index in getattr(self.view, "message_ranges", [])
        }
        markers = {"user": ">", "reasoning": "🧠", "assistant": "🤖"}
        rows = []
        for row in range(height):
            position = window_render_info.visible_line_to_row_col.get(row)
            role = starts.get(position[0]) if position and position[1] == 0 else None
            marker = markers.get(role, "")
            rows.append(
                (
                    "class:role." + role if role else "class:transcript",
                    marker + " " * max(0, width - get_cwidth(marker)) + "\n",
                )
            )
        return rows


def register_modified_enter():
    """Keep modified Enter distinct from plain CR in xterm/CSI-u terminals."""
    from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
    from prompt_toolkit.keys import Keys

    for sequence in ("\x1b[13;2u", "\x1b[27;2;13~", "\x1b[13;2~"):
        ANSI_SEQUENCES[sequence] = (Keys.Escape, Keys.ControlJ)


class SelectionWindow(Window):
    """Wheel scrolling moves the selection and its editor as one list."""

    def __init__(self, *args, move, **kwargs):
        super().__init__(*args, **kwargs)
        self.move = move

    def _scroll_up(self):
        self.move(-3)

    def _scroll_down(self):
        self.move(3)

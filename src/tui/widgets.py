"""Reusable input, completion and transcript widgets."""

from __future__ import annotations

from typing import Any, Callable

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.data_structures import Point
from prompt_toolkit.layout import Window
from prompt_toolkit.layout.controls import FormattedTextControl, UIContent, UIControl
from prompt_toolkit.layout.processors import Processor, Transformation
from prompt_toolkit.mouse_events import MouseButton, MouseEventType
from prompt_toolkit.utils import get_cwidth

from .styles import DEFAULT_ROLE_STYLES, DEFAULT_TRANSCRIPT_GUTTER_WIDTH, ICONS


class TranscriptControl(UIControl):
    """Paint only requested transcript rows; selection never rebuilds all spans."""

    def __init__(self, view):
        self.view = view
        self.rows = ()
        self._content_width = 0
        self._toolbar_rows = {}
        self._toolbar_fragments = {}
        self._pressed_interactive = None
        self.clear_selection()

    def clear_selection(self):
        self.dragging = False
        self.anchor = self.extent = None
        self.selection_rows = ()
        self.selected_text = ""
        self._pressed_interactive = None

    def _bounds(self, line):
        if not self.selected_text or self.anchor is None or self.extent is None:
            return None
        start, end = sorted((self.anchor, self.extent))
        if not start[0] <= line <= end[0]:
            return None
        return (start[1] if line == start[0] else 0, end[1] if line == end[0] else 10**9)

    def _select_to(self, point):
        if self.anchor is None or not self.selection_rows:
            return
        y = min(max(0, point.y), len(self.selection_rows) - 1)
        line = "".join(span[1] for span in self.selection_rows[y][0])
        self.extent = (y, min(max(0, point.x), len(line)))
        start, end = sorted((self.anchor, self.extent))
        lines = []
        for number in range(start[0], end[0] + 1):
            text = "".join(span[1] for span in self.selection_rows[number][0])
            value = text[
                start[1] if number == start[0] else 0 : end[1] if number == end[0] else None
            ]
            lines.append(value.rstrip() if number < end[0] else value)
        self.selected_text = "\n".join(lines) if start != end else ""
        self.view.app.invalidate()

    def is_focusable(self):
        return True

    def create_content(self, width, height):
        self.rows = self.view.transcript_rows(viewport_height=height)
        rows = self.rows
        self._content_width = width
        gutter = getattr(self.view, "transcript_gutter", None)
        if gutter is not None:
            gutter.refresh()
        self._toolbar_rows = {}
        for start, end, index in getattr(self.view, "message_ranges", ()):
            if not 0 <= index < len(self.view.messages):
                continue
            message = self.view.messages[index]
            role = message.get("role")
            if role in ("user", "assistant") and end - start > 1:
                self._toolbar_rows[end - 2] = index
        self._toolbar_fragments = {}
        cursor = self.view.transcript_cursor_position()
        cursor = Point(0, min(max(0, cursor.y), max(0, len(rows) - 1)))

        def get_line(number):
            if number in self._toolbar_rows:
                fragments = self._message_toolbar(self._toolbar_rows[number], width)
                self._toolbar_fragments[number] = fragments
                return fragments
            if hasattr(rows, "paint_row"):
                row = rows.paint_row(number)
                if row is None:
                    return [("class:sidebar.muted", " · ")]
                spans, message = row
            else:
                spans, message = rows[number]
            selected = self.view.message_selection and message == self.view.selected_message
            bounds = self._bounds(number)
            result, position = [], 0
            for span in spans:
                style, text = span[:2]
                cuts = {0, len(text)}
                if bounds:
                    cuts.update(min(len(text), max(0, value - position)) for value in bounds)
                cuts = sorted(cuts)
                for start, end in zip(cuts, cuts[1:]):
                    highlight = selected or (bounds and bounds[0] <= position + start < bounds[1])
                    result.append(
                        (
                            style + (" class:message.selected" if highlight else ""),
                            text[start:end],
                            *span[2:],
                        )
                    )
                position += len(text)
            return result

        return UIContent(
            get_line=get_line, line_count=len(rows), cursor_position=cursor, show_cursor=False
        )

    def _message_toolbar(self, index, width):
        if not 0 <= index < len(self.view.messages):
            return []
        message = self.view.messages[index]
        role = message.get("role")
        if role in ("user", "assistant"):
            actions = [("copy", ICONS["copy"].icon, self.view.tr("Copy"))]
        else:
            return []
        tab = self.view.tabs[self.view.active_tab]
        version_index = message.get("_response_version_index", 1)
        version_count = message.get("_response_version_count", 1)
        current_index = message.get("_response_version_current_index", version_count)
        viewing_previous = version_count > 1 and version_index != current_index
        if not viewing_previous and not tab.get("busy") and not tab.get("message_action_pending"):
            available = message.get("actions", ())
            for action, icon, label in (
                ("edit", ICONS["edit"].icon, "Edit message"),
                ("retry", ICONS["retry"].icon, "Retry response"),
                ("branch", ICONS["branch"].icon, "Branch chat"),
            ):
                if action in available:
                    actions.append((action, icon, self.view.tr(label)))
        if role == "assistant" and message.get("_thinking_sources"):
            actions.append(("sources", ICONS["sources"].icon, self.view.tr("Sources")))
        tab_id = tab.get("tab_id")
        has_versions = version_count > 1 and role == "assistant"
        version_label = f"{version_index}/{version_count}"
        # Transcript rows stop at this message-column width even when the pane
        # is wider. Clamp it to the actual content area so narrow panes resize
        # cleanly without letting controls escape the message box.
        paint_width = max(
            1,
            min(
                max(1, width - DEFAULT_TRANSCRIPT_GUTTER_WIDTH),
                self.view.transcript_width(),
            ),
        )
        message_width = paint_width
        prefix = " " if message_width >= 2 else ""
        toolbar_style = f"class:message.toolbar.{role}"
        settings = dict(message.get("_response_settings", ())) if role == "assistant" else {}
        model_id = settings.get("Model")
        catalog = tab.get("catalog") or getattr(self.view, "catalog", None)
        model = catalog.chat_model(model_id) if catalog and model_id else None
        model_label = (model.chat_title or model.title) if model else None
        effort = settings.get("Effort")
        if model and effort:
            effort = next((choice.title for choice in model.efforts if choice.id == effort), effort)
        settings_label = " · ".join(value for value in (model_label, effort) if value)

        def version_fragments(compact, available_width):
            if not has_versions:
                return []
            previous = ("version_prev", ICONS["back"].icon, version_index > 1)
            following = ("version_next", ICONS["forward"].icon, version_index < version_count)
            current = (None, version_label, False)
            if compact:
                single = following if following[2] else previous
                candidates = (
                    (
                        previous,
                        (None, " ", False),
                        current,
                        (None, " ", False),
                        following,
                    ),
                    (previous, current, following),
                    (current, (None, " ", False), single),
                    (current, single),
                    (single,),
                )
                values = next(
                    (
                        candidate
                        for candidate in candidates
                        if sum(get_cwidth(item[1]) for item in candidate) <= available_width
                    ),
                    (),
                )
            else:
                values = (previous, current, following)
                if sum(get_cwidth(item[1]) + 2 for item in values) > available_width:
                    return []
            result = []
            for action, label, enabled in values:
                text = label if compact else f" {label} "
                if action is None:
                    result.append(("class:message.toolbar.version", text))
                    continue

                def change_version(
                    event, selected=action, target_tab=tab_id, target_message=message
                ):
                    return self.view.message_toolbar_action(target_tab, target_message, selected)

                style = (
                    "class:message.toolbar.action"
                    if enabled
                    else "class:message.toolbar.version.disabled"
                )
                fragment = (style, text, change_version) if enabled else (style, text)
                result.append(fragment)
            return result

        def action_fragments(selected_actions, compact):
            result = [(toolbar_style, "" if compact else prefix)]
            for position, (action, icon, label) in enumerate(selected_actions):
                text = f"{icon} " if compact else f"{icon} {label}"
                if not compact:
                    text = f" {text} "

                def run_action(selected=action, target_tab=tab_id, target_message=message):
                    return self.view.message_toolbar_action(target_tab, target_message, selected)

                result.append(("class:message.toolbar.action", text, on_click(run_action)))
                if not compact and position + 1 < len(selected_actions):
                    result.append((toolbar_style, " "))
            return result

        def line_width(fragments):
            return sum(get_cwidth(fragment[1]) for fragment in fragments)

        def choose_versions(compact, selected_actions):
            action_width = line_width(action_fragments(selected_actions, compact))
            gap_width = 1 if compact and selected_actions and has_versions else 0
            return version_fragments(compact, message_width - action_width - gap_width)

        compact = False
        visible_actions = list(actions)
        fragments = action_fragments(visible_actions, compact)
        versions = choose_versions(compact, visible_actions)
        if (has_versions and not versions) or line_width(fragments + versions) > message_width:
            compact = True
            fragments = action_fragments(visible_actions, compact)
            versions = choose_versions(compact, visible_actions)
        while (
            (has_versions and not versions)
            or line_width(fragments + versions)
            + (1 if compact and visible_actions and versions else 0)
            > message_width
        ) and visible_actions:
            removable = next(
                (
                    index
                    for index in range(len(visible_actions) - 1, -1, -1)
                    if visible_actions[index][0] != "copy"
                ),
                None,
            )
            if removable is None:
                removable = len(visible_actions) - 1
            visible_actions.pop(removable)
            fragments = action_fragments(visible_actions, compact)
            versions = choose_versions(compact, visible_actions)

        used_width = line_width(fragments)
        version_width = line_width(versions)
        gap_width = 1 if compact and visible_actions and versions else 0
        available = max(0, message_width - used_width - version_width - gap_width)
        settings_text = (
            fit_text(settings_label, max(0, available - 2))
            if settings_label and available > 2
            else ""
        )
        if settings_text:
            settings_text = " " + settings_text + " "
        padding = max(0, available - get_cwidth(settings_text))
        fragments.append((toolbar_style, " " * padding))
        if settings_text:
            fragments.append(("class:message.toolbar.metadata", settings_text))
        if version_width <= message_width:
            if gap_width:
                fragments.append((toolbar_style, " "))
            fragments.extend(versions)
        trailing_width = max(0, paint_width - line_width(fragments))
        if trailing_width:
            fragments.append((toolbar_style, " " * trailing_width))
        return fragments

    def _line_fragments(self, line):
        if line in self._toolbar_rows:
            spans = self._toolbar_fragments.get(line)
            if spans is None:
                spans = self._message_toolbar(self._toolbar_rows[line], self._content_width)
                self._toolbar_fragments[line] = spans
            return spans
        if hasattr(self.rows, "paint_row"):
            row = self.rows.paint_row(line)
            return row[0] if row is not None else ()
        return self.rows[line][0]

    def _fragment_hit(self, line, x):
        spans = self._line_fragments(line)
        if not spans:
            return None
        position = 0
        for span in spans:
            end = position + get_cwidth(span[1])
            if position <= x < end:
                return position, end, span
            position = end
        return None

    @staticmethod
    def _is_interactive(span):
        if span is None or len(span) < 3:
            return False
        handler = span[2]
        return callable(handler) or bool(getattr(handler, "url", None))

    def _activate_fragment(self, line, span, mouse_event):
        handler = span[2]
        url = getattr(handler, "url", None)
        if url and hasattr(self.view, "open_message_link"):
            self.view.open_message_link(url, self.rows[line][1])
            return None
        if callable(handler):
            return handler(mouse_event)
        return None

    def mouse_handler(self, mouse_event):
        if mouse_event.button != MouseButton.LEFT:
            return NotImplemented
        line = mouse_event.position.y
        if not 0 <= line < len(self.rows):
            return NotImplemented
        if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
            self.clear_selection()
            self.view.select_message(self.rows[line][1])
            hit = self._fragment_hit(line, mouse_event.position.x)
            if line in self._toolbar_rows:
                target = hit if hit and self._is_interactive(hit[2]) else None
                self._pressed_interactive = ("toolbar", line, target)
                return None
            self.dragging = True
            self.selection_rows = self.rows
            self.anchor = (line, mouse_event.position.x)
            self.extent = self.anchor
            if hit and self._is_interactive(hit[2]):
                self._pressed_interactive = ("content", line, hit)
            return None
        if mouse_event.event_type == MouseEventType.MOUSE_MOVE and self._pressed_interactive:
            kind, pressed_line, hit = self._pressed_interactive
            if kind == "toolbar":
                return None
            if self.anchor == (line, mouse_event.position.x):
                return None
            self._pressed_interactive = None
        if (
            mouse_event.event_type == MouseEventType.MOUSE_MOVE
            and self.dragging
            and self.anchor is not None
        ):
            self._select_to(mouse_event.position)
            return None
        if mouse_event.event_type == MouseEventType.MOUSE_UP:
            pressed = self._pressed_interactive
            if pressed is not None:
                self._pressed_interactive = None
                kind, pressed_line, hit = pressed
                if kind == "toolbar":
                    if hit and line == pressed_line and hit[0] <= mouse_event.position.x < hit[1]:
                        return self._activate_fragment(pressed_line, hit[2], mouse_event)
                    return None
                if self.anchor == (line, mouse_event.position.x):
                    return self._activate_fragment(pressed_line, hit[2], mouse_event)
            self.dragging = False
            if self.anchor is not None:
                self._select_to(mouse_event.position)
            if self.selected_text:
                return None
            self.anchor = self.extent = None
            self.selection_rows = ()
            return None
        return NotImplemented


class SidebarControl(UIControl):
    """Build only the sidebar rows requested by the terminal viewport."""

    def __init__(self, view):
        self.view = view
        self.rows = ()
        self._fragments = {}
        self._states = {}
        self._open_ids = set()

    def is_focusable(self):
        return True

    def create_content(self, width, height):
        self.view._build_sidebar_rows(force=False)
        self.rows = self.view.sidebar_rows
        self._fragments = {}
        self._states = self.view._sidebar_sync_states()
        self._open_ids = {tab.get("conversation_id") for tab in self.view.tabs}
        cursor_y = min(max(0, self.view.sidebar_index), max(0, len(self.rows) - 1))
        return UIContent(
            get_line=self._line,
            line_count=len(self.rows),
            cursor_position=Point(x=0, y=cursor_y),
            show_cursor=False,
        )

    def _line(self, index):
        if not 0 <= index < len(self.rows):
            return []
        fragments = self._fragments.get(index)
        if fragments is None:
            fragments = self.view.render_sidebar_row(index, self._states, self._open_ids)
            self._fragments[index] = fragments
        # UIContent already separates rows. The eager FormattedText renderer
        # appends a newline to each row, so omit that separator here instead
        # of letting prompt_toolkit render it as a visible control character.
        return [
            (item[0], item[1][:-1] if item[1].endswith("\n") else item[1]) for item in fragments
        ]

    def mouse_handler(self, mouse_event):
        if (
            mouse_event.button != MouseButton.LEFT
            or mouse_event.event_type != MouseEventType.MOUSE_UP
        ):
            return NotImplemented
        line = mouse_event.position.y
        if not 0 <= line < len(self.rows):
            return NotImplemented
        fragments = self._fragments.get(line)
        if fragments is None:
            self._line(line)
            fragments = self._fragments[line]
        column = 0
        for fragment in fragments:
            end = column + get_cwidth(fragment[1])
            if column <= mouse_event.position.x < end and len(fragment) > 2:
                return fragment[2](mouse_event)
            column = end
        return NotImplemented


class BoundedTextControl(FormattedTextControl):
    """Clamp a dynamic cursor against the exact content snapshot being rendered."""

    def create_content(self, width, height):
        content = super().create_content(width, height)
        cursor = content.cursor_position
        if cursor is not None and content.line_count:
            y = min(max(0, cursor.y), content.line_count - 1)
            length = sum(len(part[1]) for part in content.get_line(y))
            content.cursor_position = Point(min(max(0, cursor.x), length), y)
        return content


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
        ("/attach", "add a file using the path picker"),
        ("/filter", "choose visible providers"),
        ("/account-add", "add a provider account"),
        ("/project-new", "create a project in the selected provider"),
        ("/project-edit", "edit the selected project's name and instructions"),
        ("/project-source-add", "add a supported source to the selected project"),
        ("/project-source-remove", "remove a selected project source"),
        ("/split", "split beside; choose a chat from the sidebar"),
        ("/split-down", "split below; choose a chat from the sidebar"),
        ("/unsplit", "return to a single chat pane"),
        ("/new", "start a new conversation"),
        ("/pane-next", "focus the next split pane"),
        ("/pane-prev", "focus the previous split pane"),
        ("/tab-next", "switch to the next tab"),
        ("/tab-prev", "switch to the previous tab"),
        ("/tab-close", "close the current tab"),
        ("/tabs-close", "close all tabs"),
        ("/search", "search saved conversations"),
        ("/projects", "show provider project support"),
        ("/temporary", "new temporary conversation without local recording"),
        ("/project", "move this chat or choose its project; /project info shows details"),
        ("/refresh", "synchronize models, projects, chats and history"),
        ("/model", "select a model ID for this session"),
        ("/effort", "show the provider effort value"),
        ("/actions", "search actions and commands"),
        ("/settings", "edit application and extension settings"),
        ("/downloads", "download history and progress"),
        ("/session", "account and session controls"),
        ("/login", "open login controls"),
        ("/logout", "open local logout confirmation"),
        ("/action", "run an application action by ID"),
        ("/resume", "search recent conversations"),
        ("/history", "reload this conversation"),
        ("/rename", "rename this conversation"),
        ("/delete", "delete a conversation after confirmation"),
        ("/project-delete", "delete a project and its chats after confirmation"),
        ("/copy", "copy the latest response"),
        ("/remove", "remove the latest image"),
        ("/clear", "clear the displayed transcript"),
        ("/help", "show available commands"),
        ("/exit", "quit SiopeChat"),
    )

    def __init__(self, translate=lambda source: source):
        self.translate = translate

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
                    display_meta=self.translate(description),
                )


class ComposerPlaceholderProcessor(Processor):
    def __init__(self, translate=lambda source: source):
        self.translate = translate

    def apply_transformation(self, transformation_input: Any) -> Transformation:
        fragments = transformation_input.fragments
        if transformation_input.document.text or transformation_input.lineno != 0:
            return Transformation(fragments)
        return Transformation(
            fragments + [("class:placeholder", self.translate("Mesaj yazın…"))],
            source_to_display=lambda position: position,
            display_to_source=lambda position: 0,
        )


class TranscriptWindow(Window):
    """A Window with the multi-line wheel movement users expect in terminals."""

    def __init__(
        self,
        *args: Any,
        on_manual_scroll: Callable[[], None],
        get_content_height: Callable[[], int | None] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.on_manual_scroll = on_manual_scroll
        self.get_content_height = get_content_height
        self.scroll_direction = 0

    def _last_scroll(self, info):
        # A background layout can be published before the terminal's next
        # paint. Its old render_info must not clamp the new scroll position.
        height = self.get_content_height() if self.get_content_height else None
        if height is None:
            height = info.content_height
        return max(0, height - info.window_height)

    def _scroll_up(self) -> None:
        info = self.render_info
        if info is None:
            return
        self.scroll_direction = -1
        self.on_manual_scroll()
        last_scroll = self._last_scroll(info)
        self.vertical_scroll = max(0, min(last_scroll, self.vertical_scroll) - 4)

    def _scroll_down(self) -> None:
        info = self.render_info
        if info is None:
            return
        self.scroll_direction = 1
        self.on_manual_scroll()
        last_scroll = self._last_scroll(info)
        self.vertical_scroll = min(last_scroll, self.vertical_scroll + 4)


class TranscriptGutter:
    """Render speaker markers as a line prefix inside the transcript window."""

    def __init__(self, view):
        self.view = view
        self._starts = {}

    def refresh(self):
        roles = getattr(self.view, "_transcript_roles", None)
        if roles is None:
            roles = tuple(message.get("role") for message in self.view.messages)
        self._starts = {
            start: roles[index]
            for start, _, index in getattr(self.view, "message_ranges", [])
            if 0 <= index < len(roles)
        }

    def __call__(self, line_number, wrap_count):
        role = self._starts.get(line_number) if wrap_count == 0 else None
        role_style = ICONS.get(f"role.{role}")
        marker = role_style.icon if role_style else ""
        marker_width = get_cwidth(marker)
        left = max(0, (DEFAULT_TRANSCRIPT_GUTTER_WIDTH - marker_width) // 2)
        right = max(0, DEFAULT_TRANSCRIPT_GUTTER_WIDTH - marker_width - left)
        text = " " * left + marker + " " * right
        style = f"class:{role_style.name}" if role_style else "class:transcript"
        background_style = DEFAULT_ROLE_STYLES.get(role)
        if role_style and background_style:
            style = f"class:{background_style.name} {style}"
        return [(style, text)]


def register_modified_enter():
    """Decode modified Enter and preserve shortcuts under extended keyboard modes."""
    from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
    from prompt_toolkit.keys import Keys

    for code in (9, 13, 27, 127):
        ANSI_SEQUENCES[f"\x1b[{code}u"] = ANSI_SEQUENCES[chr(code)]
        ANSI_SEQUENCES[f"\x1b[{code};1u"] = ANSI_SEQUENCES[chr(code)]
    for code in (*range(32, 127), 9, 13, 27, 127):
        for modifier in range(2, 9):
            flags = modifier - 1
            key = ANSI_SEQUENCES.get(chr(code), chr(code))
            if flags & 4 and (65 <= code <= 95 or 97 <= code <= 122 or code == 32):
                key = ANSI_SEQUENCES[chr(code & 31)]
            elif flags & 1 and 97 <= code <= 122:
                key = chr(code).upper()
            if code == 13 and flags & 1:
                key = (Keys.Escape, Keys.ControlJ)
            elif code == 9 and flags & 1:
                key = Keys.BackTab
            elif flags & 2:
                key = (Keys.Escape, key)
            for sequence in (f"\x1b[{code};{modifier}u", f"\x1b[27;{modifier};{code}~"):
                ANSI_SEQUENCES[sequence] = key
    ANSI_SEQUENCES["\x1b[13;2~"] = (Keys.Escape, Keys.ControlJ)


def install_keyboard_protocol(app):
    """Negotiate after entering the alternate screen; restore before renderer reset.

    Application has no exit event for run_async users. The renderer reset hook
    also covers resize, cancellation and exceptions, not just ChatView.run().
    """
    output = app.output
    if type(output).__name__ == "Win32Output":
        return  # The legacy Windows console cannot negotiate ANSI keyboard modes.
    enabled = False
    reset = app.renderer.reset

    def stop(*args, **kwargs):
        nonlocal enabled
        if enabled:
            output.write_raw("\x1b[<u\x1b[>4;0m")
            output.flush()
            enabled = False
        return reset(*args, **kwargs)

    def start(_app):
        nonlocal enabled
        if not enabled:
            output.write_raw("\x1b[>4;2m\x1b[>1u")
            output.flush()
            enabled = True

    app.renderer.reset = stop
    app.after_render += start


class SelectionWindow(Window):
    """Wheel scrolling moves the selection and its editor as one list."""

    def __init__(self, *args, move, **kwargs):
        super().__init__(*args, **kwargs)
        self.move = move

    def _scroll_up(self):
        self.move(-3)

    def _scroll_down(self):
        self.move(3)

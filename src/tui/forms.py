"""One keyboard/mouse form surface for files, filters, accounts and projects."""

from prompt_toolkit.application.current import get_app
from prompt_toolkit.completion import PathCompleter
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, ScrollablePane, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.mouse_events import MouseButton, MouseEventType
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Button, CheckboxList, Dialog, Frame, RadioList, TextArea


def _wrap_terminal_text(value, width):
    """Wrap text by terminal-cell width, breaking long URLs without changing them."""
    lines = []
    for paragraph in str(value or "").splitlines() or [""]:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        line = ""
        for word in words:
            if line and get_cwidth(line + " " + word) <= width:
                line += " " + word
                continue
            if line:
                lines.append(line)
                line = ""
            while get_cwidth(word) > width:
                part, used = "", 0
                for char in word:
                    cells = get_cwidth(char)
                    if part and used + cells > width:
                        break
                    part += char
                    used += cells
                lines.append(part)
                word = word[len(part) :]
            line = word
        if line:
            lines.append(line)
    return lines or [""]


class Forms:
    def __init__(self, view):
        self.view = view
        self.visible = False
        self.inline_project = None
        self.container = Window(height=0)
        self.fields = {}
        self.error = ""

    def open(
        self, title, fields, submit, *, inline_project=None, actions=None, submit_label=None
    ):
        self.inline_project = inline_project
        self.fields, body, self.error = {}, [], ""
        single_line_controls = []
        content_height = 1
        for key, label, value, choices in fields:
            body.append(
                Window(height=1, content=FormattedTextControl(label), style="class:settings.title")
            )
            if choices:
                widget = RadioList(choices, default=value)
            elif key in ("providers", "accounts"):
                widget = CheckboxList(value[0], default_values=value[1])
            else:
                widget = TextArea(
                    text=str(value),
                    multiline=key in ("instructions", "text", "files"),
                    height=8 if key == "files" else 5 if key in ("instructions", "text") else 1,
                    scrollbar=key in ("instructions", "text", "files"),
                    completer=PathCompleter(expanduser=True) if key in ("path", "files") else None,
                    style="class:settings.editor",
                )
                widget.control.focus_on_click = Condition(lambda: True)
                if key not in ("instructions", "text", "files"):
                    single_line_controls.append(widget.control)
            content_height += 2 + (
                len(choices)
                if choices
                else len(value[0])
                if key in ("providers", "accounts")
                else 8
                if key == "files"
                else 5
                if key in ("instructions", "text")
                else 1
            )
            self.fields[key] = widget
            body.append(widget)
            body.append(Window(height=1))
        body.append(
            Window(height=1, content=FormattedTextControl(lambda: self.error), wrap_lines=True)
        )

        def save():
            values = {
                key: widget.current_values
                if isinstance(widget, CheckboxList)
                else widget.current_value
                if isinstance(widget, RadioList)
                else widget.text
                for key, widget in self.fields.items()
            }
            try:
                next_step = submit(values)
            except Exception as error:
                self.error = str(error)
                self.view.app.invalidate()
                return
            self.close()
            if callable(next_step):
                next_step()

        keys = KeyBindings()
        keys.add("escape", is_global=True)(lambda _: self.close())
        keys.add("c-s", is_global=True)(lambda _: save())
        keys.add(
            "enter",
            is_global=True,
            filter=Condition(lambda: get_app().layout.current_control in single_line_controls),
        )(lambda _: save())
        pane = ScrollablePane(
            HSplit(body, padding=0, key_bindings=keys),
            height=lambda: D(
                min=1,
                preferred=min(content_height, max(1, self.view.app.output.get_size().rows - 10)),
                max=max(1, self.view.app.output.get_size().rows - 10),
            ),
        )
        if submit_label:
            submit_text = self.view.tr(submit_label)
        elif inline_project:
            submit_text = self.view.tr("Kaydet")
        else:
            submit_text = self.view.tr("Uygula")
        buttons = [
            *[Button(label, handler=handler) for label, handler in (actions or ())],
            Button(submit_text, handler=save),
            Button(
                self.view.tr("Vazgeç") if inline_project else self.view.tr("İptal"),
                handler=self.close,
            ),
        ]
        self.container = (
            HSplit(
                [
                    Window(
                        height=1, content=FormattedTextControl(title), style="class:settings.title"
                    ),
                    pane,
                    VSplit(buttons, padding=2, height=1),
                ],
                padding=1,
                width=lambda: D.exact(self.view.main_width()),
                style="class:transcript",
            )
            if inline_project
            else Dialog(title=title, body=pane, buttons=buttons, width=64, with_background=False)
        )
        self.visible = True
        self.view.app.layout.update_parents_relations()
        self.view.app.layout.focus(next(iter(self.fields.values())))
        self.view.app.invalidate()

    def open_link_list(self, title, items):
        """Show clickable links in a scrollable list without a selection step."""
        self.inline_project = None
        self.fields = {}
        self.error = ""
        terminal = self.view.app.output.get_size()
        dialog_width = max(20, min(64, terminal.columns - 8))
        line_width = max(8, dialog_width - 5)
        pane = None
        popup_frame = None
        popup_active = Condition(lambda: self.visible and self.container is popup_frame)

        def scroll_by(amount):
            pane.vertical_scroll = max(0, pane.vertical_scroll + amount)
            self.view.app.invalidate()

        def scroll(event):
            if event.event_type == MouseEventType.SCROLL_UP:
                scroll_by(-3)
                return None
            if event.event_type == MouseEventType.SCROLL_DOWN:
                scroll_by(3)
                return None
            return NotImplemented

        def source_window(item):
            fragments, line_count = [], 0

            def add_lines(value, style, handler=None):
                nonlocal line_count
                for line in _wrap_terminal_text(value, line_width):
                    if line_count:
                        fragments.append((style, "\n", scroll))
                    width = get_cwidth(line)
                    fragments.append((style, line, handler or scroll))
                    if width < line_width:
                        fragments.append(
                            ("class:dialog.body", " " * (line_width - width), scroll)
                        )
                    line_count += 1

            url = str(item.get("url") or "").strip()
            title_text = " ".join(str(item.get("title") or "").split())
            description = " ".join(str(item.get("description") or "").split())
            callback = item.get("callback")

            def activate(event, action=callback):
                if event.event_type in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
                    return scroll(event)
                if event.event_type != MouseEventType.MOUSE_UP or event.button != MouseButton.LEFT:
                    return NotImplemented
                if action is None:
                    return None
                self.close()
                action()
                return None

            if url:
                add_lines(url, "class:source.list.url", activate)
            if title_text:
                add_lines(title_text, "class:source.list.title", activate if url else None)
            if description:
                add_lines(description, "class:source.list.description")
            if not line_count:
                add_lines(self.view.tr("No sources available"), "class:source.list.description")
            return (
                Window(
                    content=FormattedTextControl(fragments),
                    width=line_width,
                    height=line_count,
                    always_hide_cursor=True,
                ),
                line_count,
            )

        def spacer_window():
            return Window(
                content=FormattedTextControl(
                    [("class:source.list.description", " " * line_width, scroll)]
                ),
                width=line_width,
                height=1,
                always_hide_cursor=True,
            )

        rows, first_focus, content_height = [], None, 0
        for index, item in enumerate(items):
            source_row, row_height = source_window(item)
            rows.append(source_row)
            content_height += row_height
            first_focus = first_focus or source_row
            if index + 1 < len(items):
                rows.append(spacer_window())
                content_height += 1
        if not rows:
            empty_row, content_height = source_window({})
            rows.append(empty_row)
            first_focus = empty_row
        pane_height = min(content_height, max(1, terminal.rows - 8))
        keys = KeyBindings()
        keys.add("escape", is_global=True, filter=popup_active)(lambda _: self.close())
        keys.add("up", is_global=True, filter=popup_active)(lambda _: scroll_by(-1))
        keys.add("down", is_global=True, filter=popup_active)(lambda _: scroll_by(1))
        keys.add("pageup", is_global=True, filter=popup_active)(
            lambda _: scroll_by(-max(1, pane_height - 1))
        )
        keys.add("pagedown", is_global=True, filter=popup_active)(
            lambda _: scroll_by(max(1, pane_height - 1))
        )
        keys.add("home", is_global=True, filter=popup_active)(
            lambda _: scroll_by(-content_height)
        )
        keys.add("end", is_global=True, filter=popup_active)(
            lambda _: scroll_by(content_height)
        )
        pane = ScrollablePane(
            HSplit(rows, padding=0, key_bindings=keys),
            keep_cursor_visible=False,
            keep_focused_window_visible=False,
            height=D.exact(pane_height),
        )
        close_button = Button(self.view.tr("Kapat"), handler=self.close)
        self.container = Frame(
            body=HSplit(
                [
                    Window(height=1),
                    VSplit(
                        [Window(width=1), pane, Window(width=1)],
                        padding=0,
                        height=pane_height,
                    ),
                    VSplit([Window(), close_button, Window()], padding=1, height=1),
                ],
                padding=0,
            ),
            title=title,
            style="class:dialog.body",
            width=dialog_width,
            height=pane_height + 4,
            modal=True,
        )
        popup_frame = self.container
        self.visible = True
        self.view.app.layout.update_parents_relations()
        self.view.app.layout.focus(first_focus or close_button)
        self.view.app.invalidate()

    def confirm(self, title, submit):
        """A destructive action needs one explicit affirmative click or keypress."""
        self.inline_project = None
        self.fields = {}
        self.error = ""

        def accept():
            self.close()
            submit()

        self.confirm_buttons = [
            Button(self.view.tr("Evet"), handler=accept),
            Button(self.view.tr("Hayır"), handler=self.close),
        ]
        keys = KeyBindings()
        keys.add("escape", is_global=True)(lambda _: self.close())
        self.container = Dialog(
            title=title,
            body=HSplit(
                [
                    Window(
                        content=FormattedTextControl(self.view.tr("Kalıcı olarak silinsin mi?")),
                        height=1,
                    )
                ],
                key_bindings=keys,
            ),
            buttons=self.confirm_buttons,
            width=54,
            with_background=False,
        )
        self.visible = True
        self.view.app.layout.update_parents_relations()
        self.view.app.layout.focus(self.confirm_buttons[1])
        self.view.app.invalidate()

    def close(self):
        self.visible = False
        self.view.app.layout.focus(
            self.view.settings_dialog.category_control
            if self.view.tab_kind == "settings"
            else self.view.downloads_panel.search
            if self.view.tab_kind == "downloads"
            else self.view.transcript_control
            if self.view.tab_kind == "project"
            else self.view.input
        )
        self.view.app.invalidate()

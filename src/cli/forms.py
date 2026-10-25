"""One keyboard/mouse form surface for files, filters, accounts and projects."""

from prompt_toolkit.application.current import get_app
from prompt_toolkit.completion import PathCompleter
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, ScrollablePane, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.widgets import Button, CheckboxList, Dialog, RadioList, TextArea


class Forms:
    def __init__(self, view):
        self.view = view
        self.visible = False
        self.inline_project = None
        self.container = Window(height=0)
        self.fields = {}
        self.error = ""

    def open(self, title, fields, submit, *, inline_project=None, actions=None):
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
        buttons = [
            *[Button(label, handler=handler) for label, handler in (actions or ())],
            Button(
                self.view.tr("Kaydet") if inline_project else self.view.tr("Uygula"), handler=save
            ),
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

    def show_message_info(self, title, *, details=(), sources=(), source_context=None):
        """Show read-only metadata for the message whose toolbar was clicked."""
        from .widgets import fit_text, on_click

        self.inline_project = None
        self.fields = {}
        self.error = ""
        body = []
        content_height = 1
        if details:
            body.append(
                Window(
                    height=1,
                    content=FormattedTextControl(self.view.tr("Answer details")),
                    style="class:settings.title",
                )
            )
            content_height += 1
            for label, value in details:
                body.append(
                    Window(
                        height=1,
                        content=FormattedTextControl(
                            [
                                ("class:metadata", f"{self.view.tr(label)} · "),
                                ("class:dialog.body", value),
                            ]
                        ),
                        wrap_lines=True,
                    )
                )
                content_height += 1
        if sources:
            body.append(Window(height=1))
            body.append(
                Window(
                    height=1,
                    content=FormattedTextControl(self.view.tr("Sources")),
                    style="class:settings.title",
                )
            )
            content_height += 2
            width = max(16, min(72, self.view.main_width() - 12))
            for source in sources:
                label = fit_text(source.get("title") or source["url"], width)
                url = source["url"]
                body.append(
                    Window(
                        height=1,
                        content=FormattedTextControl([("class:dialog.body", f"  {label}")]),
                        wrap_lines=False,
                    )
                )
                body.append(
                    Window(
                        height=1,
                        content=FormattedTextControl(
                            [
                                (
                                    "class:markdown.link",
                                    "  ↗ " + fit_text(url, max(12, width - 2)),
                                    on_click(
                                        lambda url=url: self.view.open_message_toolbar_source(
                                            *source_context, url
                                        )
                                    ),
                                )
                            ]
                        ),
                        wrap_lines=False,
                    )
                )
                content_height += 2

        self.close_button = Button(self.view.tr("Kapat"), handler=self.close)
        pane = ScrollablePane(
            HSplit(body, padding=0),
            height=lambda: D(
                min=1,
                preferred=min(content_height, max(1, self.view.app.output.get_size().rows - 10)),
                max=max(1, self.view.app.output.get_size().rows - 10),
            ),
        )
        self.container = Dialog(
            title=title,
            body=pane,
            buttons=[self.close_button],
            width=min(84, max(50, self.view.main_width() - 4)),
            with_background=False,
        )
        self.visible = True
        self.view.app.layout.update_parents_relations()
        self.view.app.layout.focus(self.close_button)
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

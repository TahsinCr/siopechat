"""One keyboard/mouse form surface for files, filters, accounts and projects."""

from prompt_toolkit.application.current import get_app
from prompt_toolkit.completion import PathCompleter
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, ScrollablePane, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.widgets import Button, CheckboxList, Dialog, RadioList, TextArea


class Forms:
    def __init__(self, view):
        self.view = view
        self.visible = False
        self.container = Window(height=0)
        self.fields = {}
        self.error = ""

    def open(self, title, fields, submit):
        self.fields, body, self.error = {}, [], ""
        single_line_controls = []
        for key, label, value, choices in fields:
            body.append(
                Window(height=1, content=FormattedTextControl(label), style="class:settings.title")
            )
            if choices:
                widget = RadioList(choices, default=value)
            elif key == "providers":
                widget = CheckboxList(value[0], default_values=value[1])
            else:
                widget = TextArea(
                    text=str(value),
                    multiline=key in ("instructions", "text"),
                    height=5 if key in ("instructions", "text") else 1,
                    scrollbar=key in ("instructions", "text"),
                    completer=PathCompleter(expanduser=True) if key == "path" else None,
                    style="class:settings.editor",
                )
                widget.control.focus_on_click = Condition(lambda: True)
                if key not in ("instructions", "text"):
                    single_line_controls.append(widget.control)
            self.fields[key] = widget
            body.append(widget)
            body.append(Window(height=1))
        body.append(
            Window(height=1, content=FormattedTextControl(lambda: self.error), wrap_lines=True)
        )

        def save():
            values = {
                key: widget.current_value
                if isinstance(widget, RadioList)
                else widget.current_values
                if isinstance(widget, CheckboxList)
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
        self.container = Dialog(
            title=title,
            body=ScrollablePane(
                HSplit(body, padding=0, key_bindings=keys),
                height=lambda: D(max=max(4, self.view.app.output.get_size().rows - 8)),
            ),
            buttons=[Button("Uygula", handler=save), Button("İptal", handler=self.close)],
            width=64,
            with_background=False,
        )
        self.visible = True
        self.view.app.layout.update_parents_relations()
        self.view.app.layout.focus(next(iter(self.fields.values())))
        self.view.app.invalidate()

    def close(self):
        self.visible = False
        self.view.app.layout.focus(
            self.view.settings_dialog.category_control
            if self.view.tab_kind == "settings"
            else self.view.input
        )
        self.view.app.invalidate()

"""Shared settings presentation; extensions contribute fields, not separate forms."""

import json

from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, VSplit, Window
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Button, TextArea

from .widgets import fit_text, on_click


class SettingsPanel:
    def __init__(self, api):
        self.api = api
        self.settings = api.settings
        self.languages = api.languages

    def rows(self) -> list[dict]:
        config = self.settings.snapshot()
        locale = config["application"]["language"]
        rows = [
            {
                "section": "application",
                "key": key,
                "title": self.languages.translate(f"application.{key}", locale),
                "value": value,
            }
            for key, value in config["application"].items()
        ]
        rows.extend(
            {
                "section": "keybindings",
                "key": key,
                "title": f"Shortcut · {key}",
                "value": value,
            }
            for key, value in sorted(config["keybindings"].items())
        )
        for row in rows:
            if row["key"] == "language":
                row["choices"] = self.languages.locales()
            elif row["key"] == "theme":
                row["choices"] = self.api.themes.available()
        rows.extend(
            {
                "section": "providers",
                "key": key,
                "title": f"Provider · {key}",
                "value": value,
                "choices": self.api.providers.ids() if key == "default" else (),
            }
            for key, value in config["providers"].items()
        )
        schemas = self.settings.fields()
        for owner in dict.fromkeys(
            (*config["extensions"], *schemas, *self.api.get("extensions").statuses())
        ):
            fields = schemas.get(owner, ())
            values = self.settings.extension(owner)
            if not any(field.key == "enabled" for field in fields):
                rows.append(
                    {
                        "section": f"extensions.{owner}",
                        "key": "enabled",
                        "title": owner,
                        "value": values.get("enabled", True),
                    }
                )
            rows.extend(
                {
                    "section": f"extensions.{owner}",
                    "key": field.key,
                    "title": self.languages.translate(field.title_key, locale),
                    "value": values[field.key],
                    "choices": field.choices,
                }
                for field in fields
            )
        return rows

    async def change(self, section: str, key: str, value) -> None:
        row = next(
            (row for row in self.rows() if row["section"] == section and row["key"] == key), None
        )
        if row is None:
            raise ValueError("unknown setting")
        if row.get("choices") and value not in row["choices"]:
            raise ValueError("choose one of the available setting values")
        if section.startswith("extensions."):
            owner = section.removeprefix("extensions.")
            if key == "enabled":
                if type(value) is not bool:
                    raise ValueError("enabled must be true or false")
                manager = self.api.get("extensions")
                manager.enable(owner) if value else await manager.disable_async(owner)
            else:
                self.settings.update_extension(owner, {key: value})
        else:
            self.settings.update(section, {key: value})


class SettingsDialog:
    """Editable schema-driven form shared by application and extension settings."""

    def __init__(self, view):
        self.view = view
        self.visible = False
        self.saving = False
        self.rows = []
        self.filtered = []
        self.index = 0
        self.message = ""
        self.category = "application"
        self.categories = [
            ("application", "Genel"),
            ("providers", "Sağlayıcılar"),
            ("accounts", "Hesaplar"),
            ("extensions", "Eklentiler"),
            ("keybindings", "Kısayollar"),
        ]
        self.category_index = 0
        self.category_control = FormattedTextControl(
            self.render_categories,
            focusable=True,
            get_cursor_position=lambda: Point(0, self.category_index * 2),
        )
        self.search = TextArea(height=1, prompt="⌕ ", multiline=False)
        self.search.buffer.on_text_changed += lambda _: self.filter()
        self.control = FormattedTextControl(
            self.render_rows, focusable=True, get_cursor_position=lambda: Point(0, self.index)
        )
        self.window = Window(
            content=self.control,
            wrap_lines=False,
            always_hide_cursor=True,
            height=lambda: D(min=3, preferred=min(10, max(3, len(self.filtered))), max=10),
            dont_extend_height=True,
        )
        self.editor = TextArea(height=1, multiline=False)
        self.save_button = Button("Kaydet", handler=self.save)
        self.cancel_button = Button("Kapat", handler=self.close)
        self.keys = KeyBindings()
        self.keys.add("escape")(lambda _: self.close())
        self.keys.add("c-s")(lambda _: self.save())
        self.keys.add("tab")(lambda event: event.app.layout.focus_next())
        self.keys.add("s-tab")(lambda event: event.app.layout.focus_previous())
        list_focus = Condition(
            lambda: self.view.app.layout.current_control in (self.control, self.search.control)
        )
        self.keys.add("up", filter=list_focus)(lambda _: self.move(-1))
        self.keys.add("down", filter=list_focus)(lambda _: self.move(1))
        self.keys.add("left", filter=list_focus)(lambda _: self.cycle_choice(-1))
        self.keys.add("right", filter=list_focus)(lambda _: self.cycle_choice(1))
        self.keys.add("enter", filter=list_focus)(lambda _: self.focus_editor())
        self.keys.add(
            "space",
            filter=Condition(
                lambda: (
                    self.view.app.layout.current_control is self.control
                    and self.current is not None
                    and type(self.current["value"]) is bool
                )
            ),
        )(lambda _: self.toggle(self.index))
        self.keys.add(
            "enter",
            filter=Condition(lambda: self.view.app.layout.current_control is self.editor.control),
        )(lambda _: self.save())
        form = HSplit(
            [
                self.search,
                self.window,
                Window(height=2, content=FormattedTextControl(self.render_detail)),
                ConditionalContainer(
                    self.editor,
                    filter=Condition(
                        lambda: self.current is not None and type(self.current["value"]) is not bool
                    ),
                ),
                Window(
                    height=1, wrap_lines=False, content=FormattedTextControl(self.render_choices)
                ),
                VSplit([self.save_button, self.cancel_button], padding=2, height=1),
                Window(
                    height=1,
                    wrap_lines=True,
                    content=FormattedTextControl(lambda: self.message),
                ),
                Window(style="class:settings", char=" "),
            ],
            modal=False,
            key_bindings=self.keys,
        )
        category_focus = Condition(
            lambda: self.view.app.layout.current_control is self.category_control
        )
        self.keys.add("up", filter=category_focus)(lambda _: self.move_category(-1))
        self.keys.add("down", filter=category_focus)(lambda _: self.move_category(1))
        self.keys.add("enter", filter=category_focus)(
            lambda _: self.choose_category(self.categories[self.category_index][0])
        )
        self.container = HSplit(
            [
                VSplit(
                    [
                        Window(content=self.category_control, width=19, style="class:settings.nav"),
                        Window(width=1, char="│", style="class:separator"),
                        ConditionalContainer(
                            form, filter=Condition(lambda: self.category != "accounts")
                        ),
                        ConditionalContainer(
                            self.view.session_dialog.container,
                            filter=Condition(lambda: self.category == "accounts"),
                        ),
                    ]
                ),
            ],
            key_bindings=self.keys,
            modal=True,
            style="class:settings",
        )

    def render_categories(self):
        return [
            (
                "class:settings.category.active"
                if key == self.category
                else "class:settings.category",
                "  " + self.view.text("category_" + key, title) + "\n\n",
                on_click(lambda key=key: self.choose_category(key)),
            )
            for key, title in self.categories
        ]

    def move_category(self, offset):
        self.category_index = (self.category_index + offset) % len(self.categories)
        self.choose_category(self.categories[self.category_index][0], focus=False)

    def choose_category(self, category, focus=True):
        self.category = category
        self.category_index = next(
            i for i, (key, _) in enumerate(self.categories) if key == category
        )
        self.view.session_dialog.visible = category == "accounts"
        self.search.text = ""
        self.filter()
        if category == "accounts":
            self.view.session_dialog.activate(focus=focus)
        elif focus:
            self.view.app.layout.focus(self.search)
        self.view.app.invalidate()

    def open(self, category="application"):
        if self.view.tab_kind != "settings":
            existing = next(
                (i for i, tab in enumerate(self.view.tabs) if tab.get("kind") == "settings"), None
            )
            if existing is not None:
                self.view.select_tab(existing)
            else:
                count = len(self.view.tabs)
                self.view.new_tab()
                if len(self.view.tabs) == count:
                    return
                self.view.tab_kind = "settings"
                self.view.title = self.view.text("settings", "Ayarlar")
                self.view._save_active_tab()
        self.view.palette_visible = self.view.resume_visible = False
        self.visible = True
        self.message = self.view.text("loading", "Yükleniyor…")
        self.save_button.text = self.view.text("save", "Kaydet")
        self.cancel_button.text = self.view.text("close", "Kapat")
        self.choose_category(category)
        self.reload()

    def reload(self):
        def loaded(rows):
            self.rows = rows
            self.message = ""
            self.filter()

        self.view._read_in_background(
            self.view.settings_rows, loaded, self.error, session_bound=False
        )

    def filter(self):
        query = self.search.text.casefold().strip()
        self.filtered = [
            row
            for row in self.rows
            if (
                row["section"].startswith("extensions.")
                if self.category == "extensions"
                else row["section"] == self.category
            )
            and all(
                token in f"{row['section']} {row.get('title', '')} {row['key']}".casefold()
                for token in query.split()
            )
        ]
        self.index = min(self.index, max(0, len(self.filtered) - 1))
        self.select(self.index, focus=False)

    @property
    def current(self):
        return self.filtered[self.index] if self.filtered else None

    def select(self, index, focus=True):
        self.index = max(0, min(index, len(self.filtered) - 1))
        row = self.current
        if row:
            value = row["value"]
            self.editor.text = (
                value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            )
            self.editor.buffer.cursor_position = len(self.editor.text)
        if focus:
            self.focus_editor()
        self.view.app.invalidate()

    def move(self, offset):
        self.select(self.index + offset, focus=False)

    def focus_editor(self):
        if self.current:
            self.view.app.layout.focus(
                self.save_button if type(self.current["value"]) is bool else self.editor
            )

    def toggle(self, index):
        self.select(index, focus=False)
        row = self.current
        if row is None or type(row["value"]) is not bool:
            return
        self.choose(not row["value"], focus=False)
        self.save()

    def render_rows(self):
        width = max(10, self.view.terminal_width() - 25)
        rows = []
        for i, row in enumerate(self.filtered):
            title = row.get("title") or row["key"]
            style = "class:palette.selected" if i == self.index else "class:palette.item"
            if type(row["value"]) is bool:
                label = fit_text(" " + title, max(1, width - 12))
                label += " " * max(1, width - 12 - get_cwidth(label))
                rows.extend(
                    [
                        (style, label, on_click(lambda i=i: self.select(i))),
                        (
                            "class:settings.enabled" if row["value"] else "class:sidebar.muted",
                            (
                                " ● " + self.view.text("enabled", "Etkin")
                                if row["value"]
                                else " ○ " + self.view.text("disabled", "Kapalı")
                            )
                            + "\n",
                            on_click(lambda i=i: self.toggle(i)),
                        ),
                    ]
                )
            else:
                label = f"{'›' if i == self.index else ' '} {title}  · {row['value']}"
                rows.append(
                    (style, fit_text(label, width) + "\n", on_click(lambda i=i: self.select(i)))
                )
        return rows or [("class:sidebar.muted", self.view.text("no_results", "Sonuç yok"))]

    def render_detail(self):
        row = self.current
        if not row:
            return ""
        descriptions = {
            "model": "Yeni sohbetlerin varsayılan modeli.",
            "effort": "Seçili modelin sunduğu düşünme seviyesi.",
            "language": "Arayüz dili.",
            "theme": "Arayüz görünümü.",
            "mouse": "Tıklama ve fare etkileşimi.",
            "show_reasoning": "Sağlayıcının sunduğu düşünme özetlerini göster.",
            "enabled": "Eklentinin özelliklerini etkinleştir veya kapat.",
        }
        description = (
            "Tuş dizisini düzenleyin."
            if row["section"] == "keybindings"
            else descriptions.get(row["key"], "")
        )
        return [
            ("class:settings.title", str(row.get("title") or row["key"]) + "\n"),
            ("class:sidebar.muted", description),
        ]

    def render_choices(self):
        row = self.current
        if not row:
            return []
        values = row.get("choices") or ((False, True) if type(row["value"]) is bool else ())
        labels = row.get("choice_labels") or {}
        if not values:
            return []
        selected = self.editor.text
        index = next(
            (
                i
                for i, value in enumerate(values)
                if str(value) == selected or json.dumps(value) == selected
            ),
            0,
        )
        width = max(8, self.view.terminal_width() - 22)
        value = values[index]
        label = (
            (self.view.text("enabled", "Etkin") if value else self.view.text("disabled", "Kapalı"))
            if type(value) is bool
            else labels.get(
                str(value),
                str(value) or self.view.text("provider_default", "Sağlayıcı varsayılanı"),
            )
        )
        return [
            ("class:button", " ‹ ", on_click(lambda: self.cycle_choice(-1))),
            (
                "class:button",
                fit_text(f"{index + 1}/{len(values)} · {label}", width),
                on_click(lambda: self.choose(value)),
            ),
            ("class:button", " › ", on_click(lambda: self.cycle_choice(1))),
        ]

    def cycle_choice(self, offset):
        row = self.current
        if not row:
            return
        values = row.get("choices") or ((False, True) if type(row["value"]) is bool else ())
        if not values:
            return
        index = next(
            (
                i
                for i, value in enumerate(values)
                if str(value) == self.editor.text or json.dumps(value) == self.editor.text
            ),
            0,
        )
        self.choose(values[(index + offset) % len(values)], focus=False)

    def choose(self, value, focus=True):
        self.editor.text = value if isinstance(value, str) else json.dumps(value)
        self.editor.buffer.cursor_position = len(self.editor.text)
        if focus:
            self.focus_editor()
        self.view.app.invalidate()

    def save(self):
        row = self.current
        if not row or self.saving:
            return
        try:
            value = (
                self.editor.text if isinstance(row["value"], str) else json.loads(self.editor.text)
            )
            if row.get("choices") and value not in row["choices"]:
                raise ValueError(
                    self.view.text("choose_listed", "Listelenen değerlerden birini seçin")
                )
        except ValueError as error:
            self.error(str(error))
            return
        self.saving = True
        self.message = self.view.text("saving", "Kaydediliyor…")

        def saved(preferences):
            self.saving = False
            self.view.apply_preferences(preferences)
            self.message = self.view.text("saved", "Kaydedildi")
            row["value"] = value
            self.reload()
            self.view.app.invalidate()

        self.view._read_in_background(
            lambda: self.view.change_setting(row["section"], row["key"], value),
            saved,
            self.error,
            session_bound=False,
        )

    def error(self, reason):
        self.saving = False
        self.message = str(reason)
        self.view.app.invalidate()

    def close(self):
        self.visible = False
        self.view.session_dialog.visible = False
        if self.view.tab_kind == "settings":
            if self.view.busy:
                self.view.switch_tab(-1)
            else:
                self.view.close_tab()
        else:
            self.view.app.layout.focus(self.view.input)
        self.view.app.invalidate()

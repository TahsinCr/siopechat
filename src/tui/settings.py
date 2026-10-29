"""Shared settings presentation; extensions contribute fields, not separate forms."""

import json

from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import DynamicContainer, HSplit, ScrollablePane, VSplit, Window
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Button, TextArea

from .actions import action_order, action_title
from .widgets import BoundedTextControl, SelectionWindow, fit_text, on_click


def SettingsPanel(api):
    from settings import SettingsCatalog

    return SettingsCatalog(api, title=action_title, order=action_order)


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
            ("application", self.view.tr("Genel")),
            ("providers", self.view.tr("Sağlayıcılar")),
            ("accounts", self.view.tr("Hesaplar")),
            ("extensions", self.view.tr("Eklentiler")),
            ("keybindings", self.view.tr("Kısayollar")),
        ]
        self.category_index = 0
        self.category_control = BoundedTextControl(
            self.render_categories,
            focusable=True,
            get_cursor_position=lambda: Point(0, 0 if self.compact() else self.category_index * 2),
        )
        self.search = TextArea(
            height=1, prompt=self.view.tr(" Ara  "), multiline=False, style="class:settings.search"
        )
        self.search.control.focus_on_click = Condition(lambda: True)
        self.search.buffer.on_text_changed += lambda _: self.filter()
        self.control = BoundedTextControl(
            self.render_rows, focusable=True, get_cursor_position=lambda: Point(0, self.index)
        )
        self.window = SelectionWindow(
            move=self.move,
            content=self.control,
            wrap_lines=False,
            always_hide_cursor=True,
            height=lambda: D.exact(self.list_height()),
            right_margins=[ScrollbarMargin(display_arrows=True)],
        )
        self.editor = TextArea(height=1, multiline=False, style="class:settings.editor")
        self.editor.control.focus_on_click = Condition(lambda: True)
        self.save_button = Button(self.view.tr("Kaydet"), handler=self.save)
        self.cancel_button = Button(self.view.tr("Kapat"), handler=self.close)
        self.view.session_dialog.close_button = self.cancel_button
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
        self.keys.add("pageup", filter=list_focus)(
            lambda _: self.move(
                -max(3, self.window.render_info.window_height - 1)
                if self.window.render_info
                else -6
            )
        )
        self.keys.add("pagedown", filter=list_focus)(
            lambda _: self.move(
                max(3, self.window.render_info.window_height - 1) if self.window.render_info else 6
            )
        )
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
                Window(height=1, char="─", style="class:separator"),
                self.window,
                Window(height=1, char="─", style="class:separator"),
                Window(height=2, content=BoundedTextControl(self.render_detail)),
                ConditionalContainer(
                    self.editor,
                    filter=Condition(
                        lambda: self.current is not None and type(self.current["value"]) is not bool
                    ),
                ),
                Window(height=1, wrap_lines=False, content=BoundedTextControl(self.render_choices)),
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
        self.keys.add("left", filter=category_focus)(lambda _: self.move_category(-1))
        self.keys.add("right", filter=category_focus)(lambda _: self.move_category(1))
        content = DynamicContainer(
            lambda: self.view.session_dialog.container if self.category == "accounts" else form
        )
        self.page = ScrollablePane(content, width=D(weight=1), height=D(weight=1))
        navigation = Window(
            content=self.category_control,
            style="class:settings.nav",
            height=lambda: 1 if self.compact() else None,
        )
        wide = VSplit(
            [
                HSplit([navigation], width=19),
                Window(width=1, char="│", style="class:separator"),
                self.page,
            ]
        )
        narrow = HSplit(
            [
                navigation,
                Window(height=1, char="─", style="class:separator"),
                self.page,
            ]
        )
        self.container = HSplit(
            [
                DynamicContainer(lambda: narrow if self.compact() else wide),
                Window(height=1, char="─", style="class:separator"),
                VSplit(
                    [
                        Window(content=BoundedTextControl(lambda: self.message), width=D(weight=1)),
                        ConditionalContainer(
                            self.save_button, filter=Condition(lambda: self.category != "accounts")
                        ),
                        self.cancel_button,
                    ],
                    padding=2,
                    height=1,
                ),
            ],
            key_bindings=self.keys,
            modal=False,
            style="class:settings",
        )

    def compact(self):
        return self.view.terminal_width() < 76

    def content_width(self):
        return max(12, self.view.terminal_width() - (0 if self.compact() else 20) - 1)

    def list_height(self):
        rows = self.view.app.output.get_size().rows if hasattr(self.view, "app") else 24
        return max(1, rows - 12 - (2 if self.compact() else 0))

    def render_categories(self):
        if self.compact():
            key, title = self.categories[self.category_index]
            return [
                ("class:button", " ‹ ", on_click(lambda: self.move_category(-1))),
                ("class:settings.category.active", self.view.text("category_" + key, title)),
                ("class:button", " › ", on_click(lambda: self.move_category(1))),
            ]
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
                self.view.title = self.view.text("settings", self.view.tr("Ayarlar"))
                self.view._save_active_tab()
        self.view.palette_visible = self.view.resume_visible = False
        self.visible = True
        self.message = self.view.text("loading", self.view.tr("Yükleniyor…"))
        self.save_button.text = self.view.text("save", self.view.tr("Kaydet"))
        self.cancel_button.text = self.view.text("close", self.view.tr("Kapat"))
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
                row["section"].startswith(self.category + ".")
                if self.category in ("extensions", "providers")
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
        width = max(10, self.content_width() - 2)
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
                                " ● " + self.view.text("enabled", self.view.tr("Etkin"))
                                if row["value"]
                                else " ○ " + self.view.text("disabled", self.view.tr("Kapalı"))
                            )
                            + "\n",
                            on_click(lambda i=i: self.toggle(i)),
                        ),
                    ]
                )
            else:
                label_width = max(8, width * 3 // 5)
                label = fit_text(" " + title, label_width)
                label += " " * max(1, label_width - get_cwidth(label))
                unassigned = row["section"] == "keybindings" and not row["value"]
                value = str(row["value"])
                if unassigned:
                    value = self.view.text("unassigned", self.view.tr("Atanmadı"))
                value = fit_text(value, max(1, width - label_width - 2))
                value_style = (
                    style + " class:sidebar.muted"
                    if unassigned
                    else style
                    + (
                        " class:settings.conflict"
                        if row.get("conflict")
                        else " class:settings.value"
                    )
                )
                rows.extend(
                    [
                        (style, label, on_click(lambda i=i: self.select(i))),
                        (
                            value_style,
                            "  " + value + "\n",
                            on_click(lambda i=i: self.select(i)),
                        ),
                    ]
                )
        return rows or [
            ("class:sidebar.muted", self.view.text("no_results", self.view.tr("Sonuç yok")))
        ]

    def render_detail(self):
        row = self.current
        if not row:
            return ""
        descriptions = {
            "model": self.view.tr("Yeni sohbetlerin varsayılan modeli."),
            "effort": self.view.tr("Seçili modelin sunduğu düşünme seviyesi."),
            "language": self.view.tr("Arayüz dili."),
            "mouse": self.view.tr("Tıklama ve fare etkileşimi."),
            "show_reasoning": self.view.tr("Sağlayıcının sunduğu düşünme özetlerini göster."),
            "enabled": self.view.tr("Eklentinin özelliklerini etkinleştir veya kapat."),
        }
        description = (
            self.view.tr("Bu eylem için bir tuş dizisi belirleyin; boş bırakabilirsiniz.")
            if row["section"] == "keybindings"
            else descriptions.get(row["key"], "")
        )
        details = [
            ("class:settings.title", str(row.get("title") or row["key"]) + "\n"),
            ("class:sidebar.muted", row["key"] + "\n" if row["section"] == "keybindings" else ""),
            ("class:sidebar.muted", description),
        ]
        if row["section"] == "keybindings" and row.get("conflict"):
            details.append(
                (
                    "class:settings.conflict",
                    self.view.text(
                        "shortcut_conflict",
                        self.view.tr("Bu tuş dizisi birden fazla eyleme atanmış."),
                    ),
                )
            )
        return details

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
            (
                self.view.text("enabled", self.view.tr("Etkin"))
                if value
                else self.view.text("disabled", self.view.tr("Kapalı"))
            )
            if type(value) is bool
            else labels.get(
                str(value),
                str(value)
                or self.view.text("provider_default", self.view.tr("Sağlayıcı varsayılanı")),
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
                    self.view.text(
                        "choose_listed", self.view.tr("Listelenen değerlerden birini seçin")
                    )
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
            self.view.close_tab()
        else:
            self.view.app.layout.focus(self.view.input)
        self.view.app.invalidate()

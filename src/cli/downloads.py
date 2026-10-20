"""Download history workspace using the application's shared download service."""

from datetime import datetime

from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import DynamicContainer, HSplit, VSplit, Window
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Button, TextArea

from .widgets import BoundedTextControl, SelectionWindow, fit_text, on_click


def size_label(size):
    value = float(size or 0)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024


class DownloadsPanel:
    def __init__(self, view):
        self.view = view
        self.records, self.rows = [], []
        self.collapsed = set()
        self.index = 0
        self.filter_index = self.sort_index = 0
        self.pending = False
        self.message = ""
        self.revision = -1
        self.filters = ("all", "active", "completed", "deleted", "failed")
        self.sorts = ("newest", "oldest", "name", "largest", "name_desc", "smallest")
        self.search = TextArea(height=1, prompt=view.tr("Ara  "), multiline=False)
        self.search.buffer.on_text_changed += lambda _: self.rebuild()
        self.control = BoundedTextControl(
            self.render, focusable=True, get_cursor_position=lambda: Point(0, self.index)
        )
        self.window = SelectionWindow(
            content=self.control,
            move=self.move,
            wrap_lines=False,
            always_hide_cursor=True,
            right_margins=[ScrollbarMargin(display_arrows=True)],
        )
        keys = KeyBindings()
        for key, callback in (
            ("up", lambda: self.move(-1)),
            ("down", lambda: self.move(1)),
            ("pageup", lambda: self.move(-10)),
            ("pagedown", lambda: self.move(10)),
            ("enter", self.activate),
        ):
            keys.add(key)(lambda event, callback=callback: callback())
        self.control.key_bindings = keys
        self.filter_button = Button("", handler=self.cycle_filter, width=23)
        self.sort_button = Button("", handler=self.cycle_sort, width=23)
        panel_keys = KeyBindings()
        panel_keys.add("tab")(lambda event: event.app.layout.focus_next())
        panel_keys.add("s-tab")(lambda event: event.app.layout.focus_previous())
        panel_keys.add("escape")(lambda _: view.close_tab())
        refresh = self.refresh_button = Button(view.tr("↻ Eşitle"), handler=self.refresh, width=14)
        actions = [
            Button(view.tr("Sohbete git"), handler=self.open_chat, width=18),
            Button(view.tr("Dosyayı sil"), handler=lambda: self.remove(True), width=18),
            Button(view.tr("Listeden kaldır"), handler=lambda: self.remove(False), width=21),
            Window(),
            Button(view.tr("Kapat"), handler=view.close_tab, width=12),
        ]
        self.action_buttons = actions
        wide_toolbar = VSplit([self.filter_button, self.sort_button, refresh])
        small_toolbar = HSplit([VSplit([self.filter_button, refresh]), self.sort_button])
        wide_actions = VSplit(actions)
        small_actions = HSplit([VSplit(actions[:2]), VSplit(actions[2:])])
        self.container = HSplit(
            [
                Window(
                    content=BoundedTextControl(
                        lambda: [("class:settings.title", "  " + view.tr("İndirmeler"))]
                    ),
                    height=1,
                ),
                self.search,
                DynamicContainer(
                    lambda: small_toolbar if view.terminal_width() < 76 else wide_toolbar
                ),
                Window(height=1, char="─", style="class:separator"),
                self.window,
                Window(
                    content=BoundedTextControl(lambda: [("class:help", self.message)]), height=1
                ),
                DynamicContainer(
                    lambda: small_actions if view.terminal_width() < 76 else wide_actions
                ),
            ],
            key_bindings=panel_keys,
        )
        self.labels()

    def footer_label(self):
        service = self.service()
        rows = service.active_snapshot() if service else []
        active = [r for r in rows if r["state"] in ("preparing", "downloading")]
        label = self.view.tr("İndirmeler")
        if active:
            size = sum(r.get("size", 0) for r in active)
            totals = [r.get("total") for r in active]
            detail = str(int(size * 100 / sum(totals))) + "%" if all(totals) else size_label(size)
            label += f" {len(active)} · {detail}"
        return " ↓ " + label + " "

    def service(self):
        api = getattr(self.view.account_service, "api", None)
        if api is None:
            return None
        try:
            return api.get("downloads")
        except LookupError:
            return None

    def labels(self):
        tr = self.view.tr
        self.search.prompt = tr("Ara  ")
        self.refresh_button.text = tr("↻ Eşitle")
        for index, label in (
            (0, "Sohbete git"),
            (1, "Dosyayı sil"),
            (2, "Listeden kaldır"),
            (4, "Kapat"),
        ):
            self.action_buttons[index].text = tr(label)
        filters = ("Tümü", "Devam eden", "Tamamlandı", "Silindi", "Başarısız / kesildi")
        sorts = ("En yeni", "En eski", "Dosya adı A–Z", "En büyük", "Dosya adı Z–A", "En küçük")
        self.filter_button.text = tr("Filtre") + ": " + tr(filters[self.filter_index])
        self.sort_button.text = tr("Sıralama") + ": " + tr(sorts[self.sort_index])

    def open(self):
        view = self.view
        existing = next(
            (i for i, tab in enumerate(view.tabs) if tab.get("kind") == "downloads"), None
        )
        if existing is not None:
            view.select_tab(existing)
        else:
            count = len(view.tabs)
            view.new_tab()
            if len(view.tabs) == count:
                return
            view.tab_kind, view.title = "downloads", view.tr("İndirmeler")
            view._save_active_tab()
        view.settings_dialog.visible = view.session_dialog.visible = False
        view.app.layout.focus(self.search)
        self.refresh()

    def refresh(self):
        service = self.service()
        if self.pending or service is None:
            return
        self.pending = True
        self.message = self.view.tr("Eşitleniyor…")

        def complete(rows):
            self.pending = False
            self.message = self.view.tr(
                "Güncel"
                if service.history_available
                else "Arşiv kapalı · geçmiş yalnız bu oturumda tutuluyor"
            )
            self.revision, self.records = service.snapshot_state()
            self.rebuild()

        def failed(reason):
            self.pending = False
            self.message = self.view.tr("İndirme geçmişi eşitlenemedi")

        self.view._read_in_background(service.refresh, complete, failed, session_bound=False)

    def update(self):
        service = self.service()
        if service and service.revision != self.revision:
            if self.view.tab_kind == "downloads":
                self.revision, self.records = service.snapshot_state()
                self.rebuild()
            else:
                self.revision = service.revision
            return True
        return False

    def cycle_filter(self):
        self.filter_index = (self.filter_index + 1) % len(self.filters)
        self.labels()
        self.rebuild()

    def cycle_sort(self):
        self.sort_index = (self.sort_index + 1) % len(self.sorts)
        self.labels()
        self.rebuild()

    def rebuild(self):
        selected = self.rows[self.index] if self.rows and self.index < len(self.rows) else None
        query, mode = self.search.text.casefold(), self.filters[self.filter_index]
        records = [
            r
            for r in self.records
            if (
                not query
                or query in (r["name"] + " " + r.get("title", "") + " " + r["account"]).casefold()
            )
            and (
                mode == "all"
                or (mode == "active" and r["state"] in ("preparing", "downloading"))
                or (mode == "failed" and r["state"] in ("failed", "interrupted"))
                or r["state"] == mode
            )
        ]
        sort = self.sorts[self.sort_index]
        records.sort(
            key=lambda r: (
                (r["name"].casefold(), r["id"])
                if sort in ("name", "name_desc")
                else (r.get("size", 0), r["created"])
                if sort in ("largest", "smallest")
                else (r["created"], r["id"])
            ),
            reverse=sort in ("newest", "largest", "name_desc"),
        )
        groups = {}
        for row in records:
            date = datetime.fromtimestamp(row["created"]).strftime("%Y-%m-%d")
            groups.setdefault(date, []).append(row)
        self.rows = []
        for date, group in groups.items():
            self.rows.append(("day", date, len(group)))
            if date not in self.collapsed:
                for row in group:
                    self.rows.extend((("file", row), ("chat", row)))
        if selected:
            identity = (selected[0], selected[1] if selected[0] == "day" else selected[1]["id"])
            self.index = next(
                (
                    i
                    for i, row in enumerate(self.rows)
                    if (row[0], row[1] if row[0] == "day" else row[1]["id"]) == identity
                ),
                self.index,
            )
        self.index = min(self.index, max(0, len(self.rows) - 1))
        if hasattr(self.view, "app"):
            self.view.app.invalidate()

    def move(self, offset):
        self.index = min(max(0, self.index + offset), max(0, len(self.rows) - 1))
        self.view.app.invalidate()

    def pick(self, index):
        self.index = index
        self.view.app.layout.focus(self.control)
        self.activate()

    def render(self):
        tr = self.view.tr
        states = {
            "preparing": "Hazırlanıyor",
            "downloading": "İndiriliyor",
            "completed": "Tamamlandı",
            "deleted": "Silindi",
            "failed": "Başarısız",
            "interrupted": "Kesildi",
        }
        if not self.rows:
            return [("class:help", "  " + tr("İndirme kaydı yok"))]
        width = max(20, self.view.terminal_width() - 4)
        result = []
        for i, row in enumerate(self.rows):
            style = "class:message.selected" if i == self.index else ""
            if row[0] == "day":
                text = f" {'▸' if row[1] in self.collapsed else '▾'} {row[1]}  · {row[2]}"
                style += " class:settings.title"
            else:
                item = row[1]
                if row[0] == "file":
                    size, total = item.get("size", 0), item.get("total")
                    detail = size_label(size)
                    if item["state"] == "downloading":
                        detail += (
                            f" / {size_label(total)} · {int(size * 100 / total)}%"
                            if total
                            else " / ?"
                        )
                        if total and width >= 95:
                            detail += " · " + tr("Kalan") + " " + size_label(max(0, total - size))
                    label = tr(states[item["state"]])
                    clock = datetime.fromtimestamp(item["created"]).strftime("%H:%M")
                    suffix = f"  · {label} · {detail}"
                    name = fit_text(
                        item["name"] or tr("Hazırlanıyor"), max(8, width - get_cwidth(suffix) - 11)
                    )
                    text = f"   {clock}  {name}{suffix}"
                else:
                    chat = (
                        tr("Sohbet silindi")
                        if item.get("chat_deleted")
                        else (item.get("title") or tr("Sohbet"))
                    )
                    if not item.get("conversation_id"):
                        chat = tr("Geçici sohbet / kaynak yok")
                    text = f"          ↳ {chat} · {item['account']}"
                    style += " class:help"
            result.append((style, fit_text(text, width), on_click(lambda i=i: self.pick(i))))
            result.append(("", "\n"))
        return result

    def activate(self):
        if not self.rows:
            return
        row = self.rows[self.index]
        if row[0] == "day":
            if row[1] in self.collapsed:
                self.collapsed.remove(row[1])
            else:
                self.collapsed.add(row[1])
            self.rebuild()
        elif row[0] == "chat":
            self.open_chat()

    def selected(self):
        if self.rows and self.rows[self.index][0] != "day":
            return self.rows[self.index][1]
        return None

    def open_chat(self):
        row = self.selected()
        if not row:
            return
        if row.get("chat_deleted") or not row.get("conversation_id"):
            self.message = self.view.tr("Sohbet silindi veya erişilemiyor")
            self.view.app.invalidate()
            return
        count = len(self.view.tabs)
        self.view.new_tab()
        if len(self.view.tabs) == count:
            return
        self.view.bind_account(row["account"])
        self.view.resume_conversation(row["conversation_id"])

    def remove(self, delete_file):
        row = self.selected()
        if not row:
            return
        if row["state"] in ("preparing", "downloading"):
            self.message = self.view.tr("İndirme sürerken kayıt silinemez")
            return

        def submit():
            def complete(_):
                self.records = self.service().snapshot()
                self.rebuild()
                self.message = self.view.tr("Silindi")

            self.view._read_in_background(
                lambda: self.service().remove(row["id"], delete_file=delete_file),
                complete,
                lambda _: setattr(self, "message", self.view.tr("Silinemedi")),
                session_bound=False,
            )

        self.view.forms.confirm(
            self.view.tr(
                "Dosya kalıcı olarak silinsin mi?"
                if delete_file
                else "Kayıt listeden kaldırılsın mı?"
            ),
            submit,
        )

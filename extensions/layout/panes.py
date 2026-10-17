"""Optional terminal split layouts; no provider operations or background app loops."""

from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.layout import DynamicContainer, HSplit, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.margins import ScrollbarMargin

from cli.markdown import MessageRenderer
from cli.widgets import TranscriptWindow, fit_text, on_click


class SplitLayout:
    def __init__(self, view):
        self.view = view
        self.groups = []
        self.pending = None
        self._panes = {}

    def active_group(self):
        existing = {tab.get("tab_id") for tab in self.view.tabs}
        for group in self.groups:
            group["tabs"] = [key for key in group["tabs"] if key in existing]
        self.groups = [group for group in self.groups if len(group["tabs"]) > 1]
        key = self.view.tabs[self.view.active_tab].get("tab_id")
        return next((group for group in self.groups if key in group["tabs"]), None)

    def begin(self, axis="columns"):
        v = self.view
        if not v.layout_enabled or v.tab_kind != "chat":
            v.status = "Bölünmüş görünüm için layout eklentisini ve bir sohbeti açın"
            return
        if v.loading_conversation or v.uploading_image:
            v.status = "Yükleme tamamlanınca bölün"
            return
        v._save_active_tab()
        group = self.active_group()
        if group and len(group["tabs"]) >= 4:
            v.status = "Bir grupta en fazla dört panel açılabilir"
            return
        self.pending = (v.tabs[v.active_tab]["tab_id"], axis)
        v.focus_sidebar()
        v.status = "Bölmek için soldan bir sohbet seçin · Esc iptal"
        v.app.invalidate()

    def attach(self):
        if not self.pending:
            return
        source, axis = self.pending
        target = self.view.tabs[self.view.active_tab]["tab_id"]
        if source == target:
            self.view.status = "Bölmek için farklı bir sohbet seçin"
            return
        group = next((g for g in self.groups if source in g["tabs"]), None)
        # A tab belongs to one group. Preserve other members when moving it.
        for old in self.groups:
            if old is not group and target in old["tabs"]:
                old["tabs"].remove(target)
        if group is None:
            group = {"axis": axis, "tabs": [source]}
            self.groups.append(group)
        if target not in group["tabs"]:
            group["tabs"].append(target)
        self.pending = None
        self.view.status = "Bölünmüş görünüm · panel başlığına tıklayarak odaklanın"
        self.view.app.invalidate()

    def unsplit(self):
        group = self.active_group()
        if group:
            self.groups.remove(group)
        self.pending = None
        self.view.app.invalidate()

    def columns(self):
        group = self.active_group()
        return min(2, len(group["tabs"])) if group and group["axis"] == "columns" else 1

    def container(self, active):
        v = self.view
        group = self.active_group() if v.layout_enabled else None
        if not group:
            return active
        signature = (group["axis"], tuple(group["tabs"]))
        if signature == getattr(self, "_layout_signature", None):
            return self._layout_container
        panes = []
        for key in group["tabs"]:
            if key not in self._panes:
                self._panes[key] = _Pane(v, key)
            pane = self._panes[key]
            body = DynamicContainer(
                lambda key=key, pane=pane: (
                    active if v.tabs[v.active_tab]["tab_id"] == key else pane.body
                )
            )
            panes.append(
                HSplit([Window(content=pane.header, height=1, style="class:pane.header"), body])
            )

        def row(items):
            children = []
            for item in items:
                if children:
                    children.append(Window(width=1, char="│", style="class:separator"))
                children.append(item)
            return VSplit(children)

        if group["axis"] == "rows":
            children = []
            for pane in panes:
                if children:
                    children.append(Window(height=1, char="─", style="class:separator"))
                children.append(pane)
            result = HSplit(children)
        elif len(panes) <= 2:
            result = row(panes)
        else:
            result = HSplit(
                [
                    row(panes[:2]),
                    Window(height=1, char="─", style="class:separator"),
                    row(panes[2:]),
                ]
            )
        self._layout_signature = signature
        self._layout_container = result
        return result


class _Pane:
    def __init__(self, view, key):
        self.view, self.key = view, key
        self.renderer = MessageRenderer()
        self.cache_key = None
        self.cached = ANSI("")
        self.follow = True
        self.header = FormattedTextControl(self.render_header)
        self.control = FormattedTextControl(self.render, get_cursor_position=self.cursor)
        self.body = TranscriptWindow(
            content=self.control,
            wrap_lines=True,
            style="class:transcript",
            char=" ",
            right_margins=[ScrollbarMargin(display_arrows=True)],
            on_manual_scroll=self.pause,
        )

    def tab(self):
        return next((tab for tab in self.view.tabs if tab.get("tab_id") == self.key), {})

    def activate(self):
        index = next(
            (i for i, tab in enumerate(self.view.tabs) if tab.get("tab_id") == self.key), None
        )
        if index is not None:
            self.view.select_tab(index)

    def pause(self):
        self.follow = False

    def render_header(self):
        tab = self.tab()
        active = self.view.tabs[self.view.active_tab].get("tab_id") == self.key
        return [
            (
                "class:tab.active" if active else "class:tab",
                fit_text(" ● " + tab.get("title", "Sohbet"), self.view.main_width()),
                on_click(self.activate),
            )
        ]

    def cursor(self):
        return Point(0, max(0, self.line_count - 1) if self.follow else self.body.vertical_scroll)

    def render(self):
        tab = self.tab()
        width = self.view.transcript_width()
        messages = tab.get("messages", [])
        key = (
            width,
            tuple((m.get("role"), m.get("text")) for m in messages),
            self.view.show_reasoning,
        )
        if key != self.cache_key:
            values = []
            for i, message in enumerate(messages):
                role = message["role"]
                if role == "reasoning" and not self.view.show_reasoning:
                    continue
                marker, color = {"user": (">", "86d49b"), "reasoning": ("🧠", "75b5ef")}.get(
                    role, ("🤖", "f0ad66")
                )
                values.append(
                    f"\x1b[38;2;{int(color[:2], 16)};{int(color[2:4], 16)};{int(color[4:], 16)}m{marker}\x1b[0m "
                    + self.renderer.render(
                        role,
                        message.get("text", ""),
                        max(8, width - 3),
                        i > 0,
                        bool(tab.get("busy")),
                    )
                )
            rendered = "".join(values)
            self.line_count = rendered.count("\n") + 1
            self.cached = ANSI(rendered)
            self.cache_key = key
        return self.cached

"""Optional terminal split layouts; no provider operations or background app loops."""

from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.layout import DynamicContainer, HSplit, VSplit, Window
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.mouse_events import MouseButton, MouseEventType

from cli.markdown import MessageRenderer, transcript_fragments
from cli.widgets import BoundedTextControl, TranscriptGutter, TranscriptWindow, fit_text, on_click


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
            v.status = self.view.tr("Bölünmüş görünüm için layout eklentisini ve bir sohbeti açın")
            return
        if v.loading_conversation or v.uploading_image:
            v.status = self.view.tr("Yükleme tamamlanınca bölün")
            return
        v._save_active_tab()
        group = self.active_group()
        if group and len(group["tabs"]) >= 4:
            v.status = self.view.tr("Bir grupta en fazla dört panel açılabilir")
            return
        self.pending = (v.tabs[v.active_tab]["tab_id"], axis)
        v.focus_sidebar()
        v.status = self.view.tr("Bölmek için soldan bir sohbet seçin · Esc iptal")
        v.app.invalidate()

    def attach(self):
        if not self.pending:
            return
        source, axis = self.pending
        target = self.view.tabs[self.view.active_tab]["tab_id"]
        if source == target:
            self.view.status = self.view.tr("Bölmek için farklı bir sohbet seçin")
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
        self.view.status = self.view.tr("Bölünmüş görünüm · panele tıklayarak odaklanın")
        self.view.app.invalidate()

    def unsplit(self):
        group = self.active_group()
        if group:
            self.groups.remove(group)
        self.pending = None
        self.view.app.invalidate()

    def focus_pane(self, offset):
        group = self.active_group()
        if not group:
            self.view.status = self.view.tr("Önce bölünmüş bir sohbet grubu açın")
            self.view.app.invalidate()
            return
        current = self.view.tabs[self.view.active_tab]["tab_id"]
        target = group["tabs"][(group["tabs"].index(current) + offset) % len(group["tabs"])]
        index = next(i for i, tab in enumerate(self.view.tabs) if tab.get("tab_id") == target)
        self.view.select_tab(index)

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
                _ClickablePane(
                    pane, [Window(content=pane.header, height=1, style="class:pane.header"), body]
                )
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


class _ClickablePane(HSplit):
    """Intercept activation across the inactive pane, including margins and blank cells."""

    def __init__(self, pane, children):
        super().__init__(children)
        self.pane = pane

    def write_to_screen(
        self, screen, mouse_handlers, write_position, parent_style, erase_bg, z_index
    ):
        super().write_to_screen(
            screen, mouse_handlers, write_position, parent_style, erase_bg, z_index
        )
        pane = self.pane
        if pane.view.tabs[pane.view.active_tab].get("tab_id") == pane.key:
            return
        wrapped = {}

        def wrap(original):
            def handler(event):
                if event.button == MouseButton.LEFT and event.event_type in (
                    MouseEventType.MOUSE_DOWN,
                    MouseEventType.MOUSE_UP,
                ):
                    if pane.view.tabs[pane.view.active_tab].get("tab_id") != pane.key:
                        pane.activate()
                    return None
                return original(event)

            return handler

        for y in range(write_position.ypos, write_position.ypos + write_position.height):
            row = mouse_handlers.mouse_handlers[y]
            for x in range(write_position.xpos, write_position.xpos + write_position.width):
                original = row[x]
                if original not in wrapped:
                    wrapped[original] = wrap(original)
                row[x] = wrapped[original]


class _Pane:
    def __init__(self, view, key):
        self.view, self.key = view, key
        self.renderer = MessageRenderer()
        self.cache_key = None
        self.cached = ANSI("")
        self.follow = True
        self.messages = []
        self.message_ranges = []
        self.header = BoundedTextControl(self.render_header)
        self.control = BoundedTextControl(self.render, get_cursor_position=self.cursor)
        self.body = TranscriptWindow(
            content=self.control,
            wrap_lines=True,
            style="class:transcript",
            char=" ",
            left_margins=[TranscriptGutter(self)],
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

    def open_link(self, url):
        self.activate()
        self.view.open_message_link(url)

    def pause(self):
        self.follow = False

    def render_header(self):
        tab = self.tab()
        active = self.view.tabs[self.view.active_tab].get("tab_id") == self.key
        return [
            (
                "class:tab.active" if active else "class:tab",
                fit_text(" ● " + tab.get("title", self.view.tr("Sohbet")), self.view.main_width()),
                on_click(self.activate),
            )
        ]

    def cursor(self):
        return Point(0, max(0, self.line_count - 1) if self.follow else self.body.vertical_scroll)

    def render(self):
        tab = self.tab()
        width = self.view.transcript_width()
        self.messages = tab.get("messages", [])
        if self.renderer.theme is not self.view.renderer.theme:
            self.renderer = MessageRenderer()
            self.renderer.theme = self.view.renderer.theme
            self.renderer.tokens = self.view.renderer.tokens
            self.cache_key = None
        key = (
            width,
            tuple((m.get("role"), m.get("text")) for m in self.messages),
            self.view.show_reasoning,
            bool(tab.get("busy")),
        )
        if key != self.cache_key:
            values, self.message_ranges, line = [], [], 0
            for i, message in enumerate(self.messages):
                role = message["role"]
                if role == "reasoning" and not self.view.show_reasoning:
                    continue
                value = self.renderer.render(
                    role,
                    message.get("text", ""),
                    width,
                    i > 0,
                    bool(tab.get("busy")) and i == len(self.messages) - 1,
                )
                end = line + value.count("\n")
                self.message_ranges.append((line, end, i))
                values.append(value)
                line = end
            self.line_count = line + 1
            self.cached = transcript_fragments("".join(values))
            self.cached = [
                (
                    span[0],
                    span[1],
                    on_click(lambda url=getattr(span[2], "url", ""): self.open_link(url)),
                )
                if len(span) > 2 and getattr(span[2], "url", None)
                else span
                for span in self.cached
            ]
            self.cache_key = key
        return self.cached

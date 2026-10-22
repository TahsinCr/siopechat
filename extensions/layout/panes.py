"""Optional terminal split layouts; no provider operations or background app loops."""

from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.layout import DynamicContainer, HSplit, VSplit, Window
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.mouse_events import MouseButton, MouseEventType

from cli.history import prepare_transcript
from cli.markdown import MessageRenderer, transcript_fragments
from cli.widgets import BoundedTextControl, TranscriptGutter, TranscriptWindow, fit_text, on_click


class SplitLayout:
    def __init__(self, view):
        self.view = view
        self.groups = []
        self.pending = None
        self._panes = {}

    def snapshot(self, name):
        """Persist references and geometry only; never credentials, drafts or jobs."""
        import json
        import uuid
        from dataclasses import asdict

        from .models import Tab, TabGroup, Workspace

        view = self.view
        view._save_active_tab()
        tabs = {}
        for tab in view.tabs:
            if (
                tab.get("kind", "chat") != "chat"
                or tab.get("temporary")
                or not tab.get("conversation_id")
            ):
                continue
            provider, account = tab["account_key"].split("/", 1)
            key = tab["tab_id"]
            tabs[key] = Tab(
                key,
                tab.get("title") or "Chat",
                provider_id=provider,
                account_id=account,
                chat_id=key,
                conversation_id=tab["conversation_id"],
            )
        active = view.tabs[view.active_tab]["tab_id"]
        groups, assigned = [], set()
        for group in self.groups:
            members = tuple(tabs[key] for key in group["tabs"] if key in tabs)
            if members:
                assigned.update(tab.id for tab in members)
                groups.append(
                    TabGroup(
                        uuid.uuid4().hex,
                        members,
                        active if active in {x.id for x in members} else members[0].id,
                        group["axis"],
                    )
                )
        for key, tab in tabs.items():
            if key not in assigned:
                groups.append(TabGroup(uuid.uuid4().hex, (tab,), key))
        active_group = next(
            (group.id for group in groups if active in {x.id for x in group.tabs}), None
        )
        return json.loads(
            json.dumps(asdict(Workspace(uuid.uuid4().hex, name, tuple(groups), active_group)))
        )

    def restore(self, value):
        from .models import Workspace

        workspace = Workspace.from_dict(value)
        view, remap = self.view, {}
        for group in workspace.groups:
            for tab in group.tabs:
                if tab.kind != "chat" or not tab.conversation_id:
                    continue
                key = tab.provider_id + "/" + tab.account_id
                if view.account_service and key not in {
                    item["key"] for item in view.account_service.items()
                }:
                    continue
                existing = next(
                    (
                        i
                        for i, state in enumerate(view.tabs)
                        if state.get("conversation_id") == tab.conversation_id
                    ),
                    None,
                )
                if existing is not None:
                    view.select_tab(existing)
                else:
                    before = len(view.tabs)
                    view.new_tab()
                    if len(view.tabs) == before:
                        continue
                    view.resume_conversation(tab.conversation_id)
                remap[tab.id] = view.tabs[view.active_tab]["tab_id"]
        for group in workspace.groups:
            members = [remap[tab.id] for tab in group.tabs if tab.id in remap]
            if len(members) > 1 and group.layout in ("columns", "rows"):
                self.groups = [
                    old for old in self.groups if not set(members).intersection(old["tabs"])
                ]
                self.groups.append({"axis": group.layout, "tabs": members})
            if group.id == workspace.active_group_id:
                active = remap.get(group.active_tab_id)
                index = next(
                    (i for i, tab in enumerate(view.tabs) if tab.get("tab_id") == active), None
                )
                if index is not None:
                    view.select_tab(index)
        view.app.invalidate()

    def active_group(self):
        existing = {tab.get("tab_id") for tab in self.view.tabs}
        for group in self.groups:
            group["tabs"] = [key for key in group["tabs"] if key in existing]
        self.groups = [group for group in self.groups if len(group["tabs"]) > 1]
        required = {key for group in self.groups for key in group["tabs"]}
        for pane_key in tuple(self._panes):
            if pane_key not in required:
                self.release(pane_key)
        key = self.view.tabs[self.view.active_tab].get("tab_id")
        return next((group for group in self.groups if key in group["tabs"]), None)

    def release(self, key):
        pane = self._panes.pop(key, None)
        if pane is not None:
            pane.dispose()
            self._layout_signature = None
            self._layout_container = None

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
        self.line_count = 1
        self._pending = None
        self._disposed = False
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
        if self._disposed:
            return []
        tab = self.tab()
        width = self.view.transcript_width()
        if self._disposed:
            return []
        self.messages = tab.get("messages", [])
        if self.renderer.theme is not self.view.renderer.theme:
            self.renderer = MessageRenderer()
            self.renderer.theme = self.view.renderer.theme
            self.renderer.tokens = self.view.renderer.tokens
            self.cache_key = None
        key = (
            width,
            self.view._virtual_message_key(self.messages),
            self.view.show_reasoning,
            bool(tab.get("busy")),
            tab.get("render_revision", 0),
            repr(self.view.renderer.tokens),
        )
        if key != self.cache_key:
            active = len(self.messages) - 1 if tab.get("busy") else None
            if self.view.app.is_running:
                if self._pending is None:
                    self._pending = key
                    messages = tuple(dict(m) for m in self.messages)
                    renderer, tokens = self.renderer, dict(self.view.renderer.tokens)

                    def cancelled():
                        return self._disposed or self.tab() is not tab

                    def ready(result):
                        self._pending = None
                        if (
                            not cancelled()
                            and self.view._virtual_message_key(tab.get("messages", [])) == key[1]
                            and tab.get("render_revision", 0) == key[4]
                            and repr(self.view.renderer.tokens) == key[5]
                            and self.renderer is renderer
                            and self.view.transcript_width() == width
                            and self.view.show_reasoning == key[2]
                            and bool(tab.get("busy")) == key[3]
                        ):
                            text, self.message_ranges, ansi, _ = result
                            self.line_count = text.count("\n") + 1
                            self._set_fragments(ansi)
                            self.cache_key = key
                        self.view.app.invalidate()

                    def failed(_reason):
                        self._pending = None
                        self.view.app.invalidate()

                    self.view._read_in_background(
                        lambda: prepare_transcript(
                            messages,
                            width,
                            tokens,
                            key[2],
                            cancelled,
                            renderer=renderer,
                            active_index=active,
                            index_rows=False,
                        ),
                        ready,
                        failed,
                        session_bound=False,
                        cpu=True,
                    )
                return self.cached
            text, self.message_ranges = self.renderer.transcript(
                self.messages, width, self.view.show_reasoning, active_index=active
            )
            self.line_count = text.count("\n") + 1
            self._set_fragments(transcript_fragments(text))
            self.cache_key = key
        return self.cached

    def _set_fragments(self, fragments):
        self.cached = [
            (
                span[0],
                span[1],
                on_click(lambda url=getattr(span[2], "url", ""): self.open_link(url)),
            )
            if len(span) > 2 and getattr(span[2], "url", None)
            else span
            for span in fragments
        ]

    def dispose(self):
        self._disposed = True
        self._pending = self.cache_key = None
        self.messages, self.message_ranges = [], []
        self.cached = ANSI("")
        self.renderer.clear_cache(wait=False)

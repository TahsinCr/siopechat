"""Optional terminal split layouts; no provider operations or background app loops."""

from prompt_toolkit.data_structures import Point
from prompt_toolkit.layout import DynamicContainer, HSplit, VSplit, Window
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.mouse_events import MouseButton, MouseEventType

from cli.history import prepare_virtual_transcript
from cli.markdown import MessageRenderer
from cli.transcript import TranscriptPrefetch, VirtualTranscriptRows
from cli.widgets import (
    BoundedTextControl,
    TranscriptControl,
    TranscriptGutter,
    TranscriptWindow,
    fit_text,
    on_click,
)


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
                        if state.get("tab_id") == tab.id
                        or (
                            state.get("account_key") == key
                            and state.get("conversation_id") == tab.conversation_id
                        )
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
            body = DynamicContainer(lambda key=key, pane=pane: self._active_body(active, key, pane))
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

    def _active_body(self, active, key, pane):
        if self.view.tabs[self.view.active_tab]["tab_id"] == key:
            pane.release_render_cache()
            return active
        return pane.body


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
        self.renderer = MessageRenderer(view.renderer.labels)
        self.cache_key = None
        self.layout = None
        self.follow = True
        self.messages = []
        self.message_ranges = []
        self._transcript_roles = ()
        self.line_count = 1
        self._pending = None
        self._disposed = False
        self._viewport_render = TranscriptPrefetch(
            lambda read, ready, failed: self.view._read_in_background(
                read, ready, failed, session_bound=False, cpu=True
            ),
            lambda: self.view.app.invalidate(),
        )
        self.header = BoundedTextControl(self.render_header)
        self.control = TranscriptControl(self)
        self.body = TranscriptWindow(
            get_content_height=lambda: self.layout.line_count if self.layout is not None else None,
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

    def transcript_cursor_position(self):
        return self.cursor()

    @property
    def app(self):
        return self.view.app

    @property
    def message_selection(self):
        return self.view.message_selection

    @property
    def selected_message(self):
        return self.view.selected_message

    def select_message(self, index):
        self.activate()
        self.view.select_message(index)

    def open_message_link(self, url, message_id=None):
        self.activate()
        self.view.open_message_link(url, message_id)

    def _current_key(self, tab, width, messages):
        return (
            width,
            self.view._virtual_message_key(messages),
            self.view.show_reasoning,
            bool(tab.get("busy")),
            tab.get("render_revision", 0),
            repr(self.view.renderer.labels),
        )

    def _release_layout(self):
        payload = getattr(self, "_render_payload", None)
        if payload is not None:
            payload.clear()
            self._render_payload = None
        layout, self.layout = self.layout, None
        if layout is not None:
            layout.dispose()
        self._viewport_render.clear()
        self.message_ranges = []
        self.line_count = 1
        self.renderer.clear_cache(wait=False)

    def release_render_cache(self):
        """Drop the inactive-pane pages when the main view owns this tab."""
        self.cache_key = None
        self._pending = None
        self._release_layout()

    def _request_layout(self, tab, key, messages, width, labels, active_index):
        if self._pending is not None:
            return
        self._pending = key
        renderer = self.renderer
        payload = {"snapshot": messages}
        self._render_payload = payload

        def cancelled():
            return (
                self._disposed
                or self.view.tabs[self.view.active_tab].get("tab_id") == self.key
                or self.tab() is not tab
                or self._current_key(tab, self.view.transcript_width(), tab.get("messages", []))
                != key
            )

        def ready(layout):
            payload.clear()
            self._pending = None
            if cancelled() or self.renderer is not renderer:
                layout.dispose()
            else:
                previous = self.layout
                if previous is not None:
                    previous.dispose(retain_renderer=True)
                self._viewport_render.clear()
                self.layout = layout
                self.cache_key = key
                self.message_ranges = layout.ranges
                self.line_count = layout.line_count
                self._transcript_roles = tuple(role for role, _text in layout.messages)
            self.view.app.invalidate()

        def failed(_reason):
            payload.clear()
            self._pending = None
            self.view.app.invalidate()

        def read():
            snapshot = payload.get("snapshot")
            if snapshot is None:
                raise InterruptedError("Pane closed")
            return prepare_virtual_transcript(
                snapshot,
                width,
                labels,
                key[2],
                cancelled,
                active_index,
                renderer,
                preserve_order=bool(tab.get("history_window")),
                previous=self.layout,
            )

        self.view._read_in_background(
            read,
            ready,
            failed,
            session_bound=False,
            cpu=True,
        )

    def _prefetch(self, layout, scroll, viewport_height):
        self._viewport_render.request(layout, scroll, viewport_height)

    def transcript_rows(self, viewport_height=1):
        if self._disposed:
            return (([("class:sidebar.muted", " · ")], None),)
        tab = self.tab()
        width = self.view.transcript_width()
        self.messages = tab.get("messages", [])
        if self.renderer.labels != self.view.renderer.labels:
            self.renderer = MessageRenderer(self.view.renderer.labels)
            self.cache_key = None
        key = self._current_key(tab, width, self.messages)
        if key != self.cache_key:
            active = len(self.messages) - 1 if tab.get("busy") else None
            messages = tuple(
                (message.get("role"), message.get("text", "")) for message in self.messages
            )
            labels = dict(self.view.renderer.labels)
            if self.view.app.is_running:
                self._request_layout(tab, key, messages, width, labels, active)
            else:
                self.layout = prepare_virtual_transcript(
                    messages,
                    width,
                    labels,
                    key[2],
                    active_index=active,
                    renderer=self.renderer,
                    preserve_order=bool(tab.get("history_window")),
                )
                self.cache_key = key
                self.message_ranges = self.layout.ranges
                self.line_count = self.layout.line_count
                self._transcript_roles = tuple(role for role, _text in messages)
        if self.layout is None:
            return (([("class:sidebar.muted", " · ")], None),)
        scroll = self.body.vertical_scroll
        if scroll > self.layout.line_count:
            scroll = max(0, self.layout.line_count - viewport_height)
        self._prefetch(self.layout, scroll, max(1, viewport_height))
        return VirtualTranscriptRows(self.layout)

    def dispose(self):
        self._disposed = True
        self._pending = self.cache_key = None
        self.messages, self.message_ranges, self._transcript_roles = [], [], ()
        self._release_layout()
        self.renderer.clear_cache(wait=False)

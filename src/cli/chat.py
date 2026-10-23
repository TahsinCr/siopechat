"""Chat view controller: state, user interaction and background callbacks."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
import time
import uuid
from datetime import datetime
from io import StringIO
from types import SimpleNamespace
from typing import Any, Callable

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import DynamicKeyBindings
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.layout.margins import ConditionalMargin, ScrollbarMargin
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import TextArea
from rich.console import Console
from rich.text import Text

from accounts import origin
from providers import Capability
from providers.history import merge_conversation
from providers.models import FeatureState, ModelCatalog
from providers.registry import PROVIDERS
from settings import DEFAULT_KEYBINDINGS
from utils import clipboard

from .actions import action_order, action_title
from .bindings import key_bindings
from .history import prepare_archive_snapshot, prepare_transcript, prepare_virtual_transcript
from .layout import render_tab_bar, workspace_layout
from .library import SORT_OPTIONS, build_sidebar_rows, merge_chat_listing
from .markdown import (
    MessageRenderer,
    index_transcript,
    render_chatgpt_annotations,
    transcript_fragments,
    visible_chat_message,
)
from .projects import ProjectPanel
from .session import SessionDialog
from .settings import SettingsDialog
from .styles import DEFAULT_TERMINAL_STYLE, terminal_style
from .widgets import (
    BoundedTextControl,
    ComposerPlaceholderProcessor,
    SidebarControl,
    SlashCommandCompleter,
    TranscriptControl,
    TranscriptWindow,
    fit_text,
    on_click,
)

Message = dict[str, str]
LoadConversation = Callable[[str], dict[str, Any]]
ListConversations = Callable[[], list[dict[str, Any]]]
SendPrompt = Callable[
    [
        str | None,
        str,
        list[dict[str, Any]],
        Callable[[dict[str, Any]], None],
        Callable[[Any], None],
        str,
        str,
    ],
    dict[str, Any],
]
UploadImage = Callable[[bytes, str, str, int, int], dict[str, Any]]
RenameConversation = Callable[[str, str], None]
SetActiveConversation = Callable[[str | None], None]


class ChatView:
    def __init__(
        self,
        list_conversations: ListConversations,
        load_conversation: LoadConversation,
        send_prompt: SendPrompt,
        conversation_id: str | None = None,
        title: str = "New conversation",
        initial_messages: list[dict[str, Any]] | None = None,
        model: str | None = None,
        upload_image: UploadImage | None = None,
        rename_conversation: RenameConversation | None = None,
        set_active_conversation: SetActiveConversation | None = None,
        keybindings: dict[str, str] | None = None,
        list_workspaces: Callable[[], list[dict[str, str]]] | None = None,
        list_actions: Callable[[], list[dict[str, str]]] | None = None,
        invoke_action: Callable[..., Any] | None = None,
        effort_preference: str = "medium",
        load_catalog: Callable[[], ModelCatalog] | None = None,
        list_projects: Callable[..., dict[str, Any]] | None = None,
        get_project: Callable[[str], dict[str, Any]] | None = None,
        list_project_chats: Callable[..., dict[str, Any]] | None = None,
        initial_project: dict[str, Any] | None = None,
        refresh_on_start: bool = False,
        settings_rows=None,
        change_setting=None,
        load_preferences=None,
        session_action=None,
        list_history_page=None,
        forget_temporary=None,
        move_conversation=None,
        layout_factory=None,
        account_service=None,
        action_host=None,
    ) -> None:
        from .widgets import install_keyboard_protocol, register_modified_enter

        register_modified_enter()
        self.account_service = account_service
        self.account_key = account_service.active() if account_service else "chatgpt/default"
        self.provider_id = self.account_key.split("/")[0]
        self.provider_features = frozenset({Capability.PROJECTS})
        self.provider_filter = None
        self.account_filter = None
        self.library_sort = "time_desc"
        self._account_projects = {}
        self._account_catalogs = {}
        self._catalog_requests = {}
        self._project_requests = {}
        self._archive_revision = -1
        self._archive_page_ids = {}
        self._model_picker_requests = set()
        self.attach_file_callback = None
        self.attachment_policy_callback = None
        self.settings_rows_callback = settings_rows
        self.change_setting = change_setting
        self.load_preferences = load_preferences
        self.session_action = session_action
        self.session_state = {}
        self.session_epoch = 0
        self.translations = {}
        self.mouse_enabled = True
        self.list_history_page = list_history_page
        self.forget_temporary = forget_temporary
        self.move_conversation = move_conversation
        self.history_cursor = None
        self.history_loading = False
        self.list_conversations = list_conversations
        self.load_conversation = load_conversation
        self.send_prompt = send_prompt
        self.upload_image = upload_image
        self.rename_conversation = rename_conversation
        self.set_active_conversation = set_active_conversation
        self.keybindings = dict(DEFAULT_KEYBINDINGS) | (keybindings or {})
        self.list_workspaces = list_workspaces or (lambda: [])
        self.list_actions = list_actions or (lambda: [])
        self.invoke_action = invoke_action
        self.conversation_id = conversation_id
        self.title = title
        self.model = model or os.environ.get("CHATGPT_WEB_MODEL", "")
        self.effort_preference = effort_preference
        self.effort: str | None = None
        self.load_catalog = load_catalog
        self.list_projects_callback = list_projects
        self.get_project_callback = get_project
        self.list_project_chats_callback = list_project_chats
        self.catalog = ModelCatalog(FeatureState.UNKNOWN)
        self.catalog_error: str | None = None
        self.catalog_loading = False
        self.project_items: dict[str, dict[str, Any]] = {}
        self.project_cursor: str | None = None
        self.project_error: str | None = None
        self.projects_loading = False
        self.projects_loaded = False
        self.project_chats_loaded: set[str] = set()
        self.project_chats_loading: set[str] = set()
        self.project_chat_cursors: dict[str, str | None] = {}
        self.project_chat_errors: dict[str, str] = {}
        self.selected_message = 0
        self.message_selection = False
        self.show_reasoning = True
        self.message_ranges = []
        self.temporary = False
        self.tab_kind = "chat"
        self.last_response_model: str | None = None
        self.current_project: dict[str, Any] = dict(initial_project or {})
        self.messages: list[Message] = []
        for message in initial_messages or []:
            visible = visible_chat_message(message)
            if visible is not None:
                self.messages.append(visible)
        self.background_tabs: list[dict[str, Any]] = []
        self.busy = False
        self.busy_tab_index: int | None = None
        self.background_jobs = 0
        self.loading_conversation = False
        self.loading_started: float | None = None
        self.load_on_start = conversation_id is not None and (
            not initial_messages or refresh_on_start
        )
        self.uploading_image = False
        self.upload_started: float | None = None
        self.pending_attachments: list[dict[str, Any]] = []
        self._attachments_in_flight = False
        self._inflight_attachments: list[dict[str, Any]] = []
        self.status = "Ready"
        self.cancel_connection: Any = None
        self.stop_confirmation_until = 0.0
        self.stop_confirmation_tab_id: str | None = None
        from .background import BackgroundReads

        self._reads = BackgroundReads()
        self._render_reads = BackgroundReads(workers=2, capacity=16)
        self._operations = BackgroundReads(workers=2, capacity=16)
        self.lock = threading.RLock()
        self.resume_visible = False
        self.resume_loading = False
        self.resume_items: list[dict[str, Any]] = []
        self.resume_filtered: list[dict[str, Any]] = []
        self.resume_index = 0
        self.resume_filter = Condition(lambda: self.resume_visible)
        self.follow_output = True
        self.transcript_line_count = 1
        self.active_started: float | None = None
        self.last_elapsed: float | None = None
        self.renderer = MessageRenderer()
        from .paging import HistoryPager

        self.history_pager = HistoryPager(self)
        self.event_loop: asyncio.AbstractEventLoop | None = None
        self.sidebar_items: list[dict[str, Any]] = []
        self.sidebar_index = 0
        self.sidebar_error: str | None = None
        self.workspace_error: str | None = None
        self.sidebar_loading = False
        self.sidebar_refresh_generation = 0
        self.sidebar_visible = True
        self.sidebar_collapsed: set[str] = set()
        self._sidebar_known_projects: set[str] = set()
        self.sidebar_rows: list[dict[str, Any]] = []
        self._sidebar_rows_dirty = True
        self.palette_visible = False
        self.palette_mode = "actions"
        self.palette_items: list[dict[str, str]] = []
        self.palette_filtered: list[dict[str, str]] = []
        self.palette_index = 0
        self.sidebar_visible_filter = Condition(lambda: self.sidebar_visible)
        self.palette_filter = Condition(lambda: self.palette_visible)
        self.tabs: list[dict[str, Any]] = [dict(self._new_tab_state(), tab_id=uuid.uuid4().hex)]
        self.active_tab = 0

        self.transcript_control = TranscriptControl(self)
        self.transcript_window = TranscriptWindow(
            content=self.transcript_control,
            wrap_lines=False,
            right_margins=[],
            allow_scroll_beyond_bottom=False,
            on_manual_scroll=self.pause_following,
        )
        self.input = TextArea(
            height=lambda: self.composer_height(),
            multiline=True,
            wrap_lines=True,
            prompt=self.render_prompt,
            dont_extend_height=True,
            completer=SlashCommandCompleter(self.tr),
            complete_while_typing=True,
            input_processors=[ComposerPlaceholderProcessor(self.tr)],
            read_only=Condition(self.editing_project),
            style="class:composer",
            scrollbar=False,
        )
        self.input.window.right_margins = [
            ConditionalMargin(
                ScrollbarMargin(display_arrows=True),
                filter=Condition(lambda: self.composer_line_count() > 6),
            )
        ]
        self.input.control.focus_on_click = Condition(lambda: True)
        self.resume_search = TextArea(
            height=1,
            multiline=False,
            prompt=self.tr("Search: "),
        )
        self.resume_search.buffer.on_text_changed += lambda _buffer: self.filter_resume()
        self.sidebar_search = TextArea(
            height=1, prompt=self.tr(" Ara "), style="class:sidebar.search"
        )
        self.sidebar_search.buffer.on_text_changed += lambda _buffer: self.filter_sidebar()
        self.sidebar_control = SidebarControl(self)
        self.sidebar_window = Window(
            content=self.sidebar_control, wrap_lines=False, always_hide_cursor=True
        )
        self.palette_search = TextArea(height=1, prompt="⌕ ", style="class:palette.search")
        self.palette_search.buffer.on_text_changed += lambda _buffer: self.filter_palette()
        self.palette_control = BoundedTextControl(
            text=self.render_palette,
            focusable=True,
            get_cursor_position=lambda: Point(x=0, y=self.palette_index),
        )
        self.palette_window = Window(
            content=self.palette_control, wrap_lines=False, always_hide_cursor=True
        )
        self.resume_list_control = BoundedTextControl(
            text=self.render_resume_list,
            focusable=True,
            get_cursor_position=lambda: Point(x=0, y=self.resume_index),
        )
        self.resume_list_window = Window(
            content=self.resume_list_control,
            wrap_lines=False,
            always_hide_cursor=True,
        )
        from .forms import Forms

        self.forms = Forms(self)
        self.project_panel = ProjectPanel(self)
        self.session_dialog = SessionDialog(self)
        self.settings_dialog = SettingsDialog(self)
        from .downloads import DownloadsPanel

        self.downloads_panel = DownloadsPanel(self)
        self.layout_enabled = True
        self.layout_factory = layout_factory
        self.layout_controller = layout_factory(self) if layout_factory else None
        self.bindings = key_bindings(self)
        self.app: Application[Any] = Application(
            layout=Layout(workspace_layout(self), focused_element=self.input),
            key_bindings=DynamicKeyBindings(lambda: self.bindings),
            full_screen=True,
            mouse_support=Condition(lambda: self.mouse_enabled),
            min_redraw_interval=0.05,
            max_render_postpone_time=0.1,
            style=Style.from_dict(DEFAULT_TERMINAL_STYLE),
        )

        install_keyboard_protocol(self.app)
        if self.account_service:
            self.bind_account(self.account_key)
            self.restore_account_preference()
            self._save_active_tab()

        self.action_cleanup = action_host(self) if action_host else None

    async def animate_activity(self) -> None:
        """Refresh animated status only while an operation is active.

        A permanent prompt_toolkit refresh interval repaints the terminal even
        when the UI is idle. Those writes clear native terminal selections,
        making copied text lose its highlight almost immediately.
        """
        last_account_check = 0.0
        last_download_check = 0.0
        while True:
            await asyncio.sleep(0.12)
            now = asyncio.get_running_loop().time()
            if self.account_service and now - last_account_check >= 5:
                last_account_check = now
                key = self.account_key
                try:
                    state = await asyncio.to_thread(self.account_service.status, key)
                except ValueError:
                    # A local account can be removed between the timer and UI callback.
                    continue
                if key == self.account_key and state != self.session_state:
                    self.session_state = state
                    self.app.invalidate()
            downloads_changed = self.downloads_panel.update()
            if now - last_download_check >= 10:
                last_download_check = now
                self.downloads_panel.refresh()
            if downloads_changed:
                self.app.invalidate()
            active_tab_busy = bool(self.tabs and self.tabs[self.active_tab].get("busy"))
            self.apply_archive_updates()
            if active_tab_busy or self.loading_conversation or self.uploading_image:
                self.app.invalidate()

    def archive_service(self):
        if self.account_service and hasattr(self.account_service, "archive"):
            return self.account_service.archive()
        return None

    def sync_states(self, archive):
        signature = (id(archive), archive.revision)
        if signature != getattr(self, "_sync_state_signature", None):
            self._sync_state_rows = archive.states()
            self._sync_state_signature = signature
        return self._sync_state_rows

    def synchronize(self, scope="all"):
        if getattr(self, "_synchronize_pending", False):
            self.status = self.tr("Eşitleme zaten sürüyor")
            return
        if not self.account_service or not hasattr(self.account_service, "synchronize"):
            self.refresh_sidebar()
            self.refresh_provider_catalog(force=True)
            return
        self._synchronize_pending = True
        key, cid = self.account_key, self.conversation_id
        self.status = self.tr("Eşitleniyor…")

        def finish(error=None):
            self._synchronize_pending = False
            self.status = (
                self.tr("Eşitleme başlatıldı") if error is None else self.tr("Eşitleme: ") + error
            )
            self.refresh_sidebar()
            if scope == "all":
                self.refresh_provider_catalog()
            self.app.invalidate()

        self._read_in_background(
            lambda: self.account_service.synchronize(
                scope, key if scope == "conversation" else None, cid
            ),
            lambda _: finish(),
            lambda reason: finish(reason),
            session_bound=False,
        )

    def refresh_open_conversation(self):
        if not self.account_service or not self.conversation_id or not self.archive_service():
            return
        conversation_id, key = self.conversation_id, self.account_key
        self._read_in_background(
            lambda: self.account_service.load(conversation_id, key),
            lambda _: None,
            lambda reason: setattr(
                self, "status", self.tr("Sohbet senkronize edilemedi: ") + reason
            ),
            session_bound=False,
        )

    def apply_archive_updates(self, snapshot=None):
        source = self.archive_service()
        archive = snapshot or source
        revision = (id(source), archive.revision) if archive is not None else None
        if archive is None or revision == self._archive_revision:
            return
        if snapshot is None and self.app.is_running:
            if getattr(self, "_archive_read_pending", False):
                return
            self._archive_read_pending = True
            accounts = self.account_service.active_accounts()
            tabs = [
                (
                    tab,
                    dict(
                        ((key, value) for key, value in tab.items() if key != "messages"),
                        follow_output=self.follow_output
                        if tab is self.tabs[self.active_tab]
                        else tab.get("follow_output", True),
                    ),
                )
                for tab in self.tabs
            ]
            known_chats, known_projects = list(self.sidebar_items), list(self.project_items)
            default_account = self.account_key

            def read():
                return prepare_archive_snapshot(
                    source,
                    accounts,
                    tabs,
                    known_chats,
                    known_projects,
                    default_account,
                    revision[1],
                )

            def completed(result):
                self._archive_read_pending = False
                if self.archive_service() is source:
                    self.apply_archive_updates(result)

            def failed(reason):
                self._archive_read_pending = False
                self.status = self.tr("Arşiv okunamadı: ") + str(reason)
                self.app.invalidate()

            self._read_in_background(read, completed, failed, session_bound=False)
            return
        self._archive_revision = revision
        accounts = self.account_service.active_accounts()
        chats = {item["id"]: item for item in self.sidebar_items if item.get("id")}
        for account in accounts:
            key = account["key"]
            page = archive.get(key, "chats", "")
            if page:
                new_ids = {item["id"] for item in page["items"]}
                prior_ids = self._archive_page_ids.get((key, "chats"), set())
                if page.get("complete"):
                    prior_ids = prior_ids | {
                        cid
                        for cid, item in chats.items()
                        if item.get("context") == key
                        and not item.get("project_id")
                        and item.get("kind") != "workspace"
                    }
                for removed in prior_ids - new_ids:
                    chats.pop(removed, None)
                self._archive_page_ids[key, "chats"] = new_ids
                for item in page["items"]:
                    chats[item["id"]] = merge_chat_listing(chats.get(item["id"]), item)
            page = archive.get(key, "containers", "")
            if page:
                new_ids = {item["id"] for item in page["items"]}
                for removed in self._archive_page_ids.get((key, "containers"), set()) - new_ids:
                    self.project_items.pop(removed, None)
                    for projects in self._account_projects.values():
                        projects.pop(removed, None)
                self._archive_page_ids[key, "containers"] = new_ids
                for item in page["items"]:
                    self.project_items[item["id"]] = item
        for cid in list(chats):
            key, remote = origin(cid, self.account_key)
            if archive.deleted(key, "conversation", remote):
                chats.pop(cid, None)
        for pid in list(self.project_items):
            key, remote = origin(pid, self.account_key)
            if archive.deleted(key, "container", remote):
                self.project_items.pop(pid, None)
        self.sidebar_items = list(chats.values())
        states = {(x["account"], x["kind"], x["remote"]): x for x in self.sync_states(archive)}
        for i, tab in enumerate(self.tabs):
            if not tab.get("conversation_id"):
                continue
            key, remote = origin(tab["conversation_id"], tab.get("account_key", self.account_key))
            if archive.deleted(key, "conversation", remote):
                tab["syncing"] = False
                tab["load_error"] = self.tr("Sağlayıcıdan silinmiş sohbet")
                if i == self.active_tab:
                    self.status = self.tr("Bu sohbet sağlayıcıdan silinmiş · yeni sohbet açın")
                continue
            state = states.get((key, "conversation", remote))
            tab["syncing"] = bool(state and state["state"] == "syncing")
            if tab.get("busy"):
                continue
            if (
                not state
                or state["state"] != "ready"
                or tab.get("archive_version") == state.get("version")
            ):
                continue
            if snapshot is not None and snapshot.tab_windows.get(id(tab)) is not tab.get(
                "history_window"
            ):
                self._archive_revision = -1
                continue
            window = tab.get("history_window") or {}
            value = (
                archive.get(key, "conversation", remote)
                if snapshot is not None
                else archive.conversation_window(
                    key,
                    remote,
                    start=None if tab.get("follow_output", True) else window.get("start"),
                )
            )
            if not value:
                continue
            tab["archive_version"] = state["version"]
            from .paging import visible_window

            messages = visible_window(value, tab.get("messages", ()))
            if tab.get("messages") != messages:
                if i == self.active_tab and not self.follow_output:
                    tab["history_anchor"] = self.history_pager.anchor(tab)
                tab["messages"] = messages
                tab["history_window"] = value.get("history_window")
                tab["title"] = value.get("title") or tab.get("title")
                if i == self.active_tab:
                    self.messages = messages
                    self.title = tab["title"]
        self._invalidate_sidebar_rows()
        self.app.invalidate()

    def render_prompt(self) -> FormattedText:
        return FormattedText([("class:prompt", " › ")])

    def restore_account_preference(self):
        if self.account_service and hasattr(self.account_service, "preference"):
            preference = self.account_service.preference(self.account_key)
            if preference.get("model"):
                self.model = preference["model"]
                self.effort = preference.get("effort")
                self.effort_preference = self.effort or ""
        self.normalize_model_selection()

    def remember_selection(self):
        if self.account_service and self.account_key and hasattr(self.account_service, "remember"):
            self.account_service.remember(self.account_key, self.model, self.effort)

    def bind_account(self, key):
        if self.account_service is None:
            return
        if key != self.account_key:
            self._account_projects[self.account_key] = self.project_items
            self.project_items = dict(self.project_items) | self._account_projects.get(key, {})
            self.projects_loaded = bool(self.project_items)
            self._invalidate_sidebar_rows()
        self.account_key = key
        self.provider_id = key.split("/")[0]
        self.provider_features = self.account_service.capabilities(key) if key else frozenset()
        known = self._account_catalogs.get(key)
        if key and known is None and hasattr(self.account_service, "cached_models"):
            known = self.account_service.cached_models(key)
        self.catalog = known or ModelCatalog(FeatureState.UNKNOWN)
        if known:
            self._account_catalogs[key] = known
        self.catalog_error = self.catalog.reason
        self.catalog_loading = key in self._catalog_requests
        self.projects_loading = key in self._project_requests
        for name, callback in self.account_service.callbacks(key).items():
            setattr(self, name, callback)
        self.session_state = self.account_service.status(key)
        if not key:
            self.set_active_conversation = lambda value: None
        elif hasattr(self.account_service, "set_active"):
            self.set_active_conversation = lambda value: (
                self.account_service.queue_active(key, value)
                if hasattr(self.account_service, "queue_active")
                else self.account_service.set_active(key, value)
            )
        else:
            self.set_active_conversation = lambda value: self.account_service.cache(
                key
            ).set_active_conversation(origin(value, key)[1])
        self.list_conversations = lambda: self.account_service.list_chats(
            self.provider_filter, account_keys=self.account_filter
        )["items"]
        self.list_history_page = lambda cursor: self.account_service.list_chats(
            self.provider_filter, cursor, account_keys=self.account_filter
        )

    def select_account(self, key):
        if (
            self.account_service
            and hasattr(self.account_service, "enabled")
            and not self.account_service.enabled(key.split("/")[0])
        ):
            self.status = self.tr("Sağlayıcı devre dışı · Ayarlar → Sağlayıcılar")
            self.app.invalidate()
            return False
        if (
            self.account_service is None
            or self.loading_conversation
            or self.uploading_image
            or self.session_dialog.loading
        ):
            return False
        if self.tab_kind == "chat" and (self.messages or self.conversation_id or self.input.text):
            self.new_tab()
        self.bind_account(key)
        self.model = self.catalog.default_chat_model.id if self.catalog.default_chat_model else ""
        self.effort = self.catalog.effort(self.model, self.effort_preference)
        self.restore_account_preference()
        self.current_project = {}
        self.projects_loaded = False
        self.account_service.select(key)
        self._save_active_tab()
        self.refresh_sidebar()
        self.refresh_provider_catalog()
        self.app.invalidate()
        return True

    def add_account(self):
        if not self.account_service:
            return

        def submit(values):
            key = self.account_service.add(values["provider"], values["label"])
            self.select_account(key)
            self.settings_dialog.open("accounts")

        self.forms.open(
            self.tr("Hesap ekle"),
            [
                (
                    "provider",
                    self.tr("Sağlayıcı"),
                    "chatgpt",
                    [(key, item[0]) for key, item in PROVIDERS.items()],
                ),
                ("label", self.tr("Hesap adı"), "", None),
            ],
            submit,
        )

    def edit_account(self):
        if not self.account_service or not self.account_key:
            return
        key = self.account_key
        item = self.account_service.item(key)

        def submit(values):
            try:
                updated = self.account_service.rename(key, values["label"])
            except ValueError as error:
                raise ValueError(self.tr(str(error))) from error
            if self.account_key == key:
                self.session_state = dict(self.session_state, account=updated["label"])
            self.app.invalidate()

        self.forms.open(
            self.tr("Hesabı düzenle"),
            [("label", self.tr("Hesap adı"), item["label"], None)],
            submit,
        )

    def open_filters(self):
        if not self.account_service:
            return
        accounts = [a for a in self.account_service.items() if a.get("enabled", True)]
        if not accounts:
            return

        def apply(values):
            if not values["accounts"]:
                raise ValueError(self.tr("En az bir hesap seçin"))
            self.account_filter = set(values["accounts"])
            self.provider_filter = {
                a["provider"] for a in accounts if a["key"] in self.account_filter
            }
            self.library_sort = values["sort"]
            self._invalidate_sidebar_rows()
            self.refresh_sidebar()

        self.forms.open(
            self.tr("Kütüphane filtresi ve sıralama"),
            [
                (
                    "accounts",
                    self.tr("Gösterilecek hesaplar"),
                    (
                        [
                            (a["key"], f"{a['label']} · {PROVIDERS[a['provider']][0]}")
                            for a in accounts
                        ],
                        list(
                            self.account_filter
                            if self.account_filter is not None
                            else [a["key"] for a in accounts]
                        ),
                    ),
                    None,
                ),
                (
                    "sort",
                    self.tr("Sohbet ve proje sıralaması"),
                    self.library_sort,
                    [(key, self.tr(title)) for key, title in SORT_OPTIONS],
                ),
            ],
            apply,
        )

    def delete_record(self, kind="conversation", value=None):
        if not self.account_service:
            self.status = self.tr("Silme için sağlayıcı hesabı gerekli")
            return
        if value is None and self.app.layout.current_control is self.sidebar_control:
            self._build_sidebar_rows()
            if self.sidebar_rows:
                row = self.sidebar_rows[self.sidebar_index]
                if (kind == "container" and row.get("kind") == "project") or (
                    kind == "conversation" and row.get("kind") == "chat"
                ):
                    value = row.get("id")
        if value is None:
            value = (
                (self.current_project or {}).get("id")
                if kind == "container"
                else self.conversation_id
            )
        if not value:
            self.status = self.tr("Önce silinecek sohbeti veya projeyi seçin")
            return
        key, _ = origin(value, self.account_key)
        capability = Capability.DELETE if kind == "conversation" else Capability.PROJECT_DELETE
        if capability not in self.account_service.capabilities(key):
            self.status = self.tr("Sağlayıcı bu silme işlemini desteklemiyor")
            return
        title = (
            self.tr("Projeyi ve içindeki sohbetleri sil")
            if kind == "container"
            else self.tr("Sohbeti sil")
        )

        def submit():
            self.status = self.tr("Sağlayıcıdan siliniyor…")

            def success(result):
                removed = set(result["removed"])
                self.sidebar_items = [
                    x
                    for x in self.sidebar_items
                    if x.get("id") not in removed and x.get("project_id") not in removed
                ]
                self._invalidate_sidebar_rows()
                for item in removed:
                    self.project_items.pop(item, None)
                    self.project_chats_loaded.discard(item)
                    for projects in self._account_projects.values():
                        projects.pop(item, None)
                # Keep tabs and drafts, but never allow a deleted remote ID to be reused.
                for tab in self.tabs:
                    if (
                        tab.get("conversation_id") in removed
                        or tab.get("project", {}).get("id") in removed
                    ):
                        tab.update(
                            kind="chat",
                            conversation_id=None,
                            messages=[],
                            project={},
                            title=self.tr("Yeni sohbet"),
                            syncing=False,
                        )
                self._restore_active_tab(focus=False)
                self.filter_sidebar()
                self.status = self.tr("Sağlayıcıdan silindi")
                self.app.invalidate()

            self._read_in_background(
                lambda: self.account_service.delete(value, key, kind),
                success,
                lambda reason: setattr(self, "status", self.tr("Silme işlemi: ") + reason),
                session_bound=False,
            )

        self.forms.confirm(title, submit)

    def open_project_create(self):
        self.project_panel.create()

    def open_attachment(self):
        from .attachments import AttachmentPicker

        AttachmentPicker(self).open()

    def _release_staged_attachments(self, attachments):
        release = getattr(self.account_service, "release_attachment", None)
        if not callable(release):
            return
        for attachment in attachments:
            try:
                release(attachment)
            except OSError:
                continue

    def _new_tab_state(self) -> dict[str, Any]:
        return {
            "account_key": self.account_key,
            "catalog": self.catalog,
            "conversation_id": self.conversation_id,
            "kind": self.tab_kind,
            "title": self.title,
            "messages": self.messages,
            "model": self.model,
            "effort": self.effort,
            "response_model": self.last_response_model,
            "effort_preference": self.effort_preference,
            "draft": self.input.text if hasattr(self, "input") else "",
            "project": self.current_project,
            "temporary": self.temporary,
            "follow_output": self.follow_output,
            "scroll": self.transcript_window.vertical_scroll
            if hasattr(self, "transcript_window")
            else 0,
            "busy": self.active_busy(),
            "completed_pending": False,
            "loading": self.loading_conversation,
            "loading_started": self.loading_started,
        }

    def tr(self, source):
        from language import literal

        return literal(self.translations, source)

    def text(self, key, fallback):
        return self.translations.get(f"application.{key}", fallback)

    def render_tabs(self):
        return render_tab_bar(self)

    def close_clicked_tab(self, index):
        self.select_tab(index)
        if self.active_tab == index:
            self.close_tab()

    def select_tab(self, index):
        self.switch_tab(index - self.active_tab)

    def editing_project(self):
        return bool(
            self.tab_kind == "project"
            and self.forms.visible
            and self.forms.inline_project == self.current_project.get("id")
        )

    def render_project_actions(self):
        return FormattedText(
            [
                (
                    "class:button",
                    self.tr(" ✎ Düzenle "),
                    on_click(lambda: self.project_panel.edit(self.current_project.get("id"))),
                ),
                ("class:button", self.tr(" ＋ Kaynak "), on_click(self.project_panel.add_source)),
                ("class:button", self.tr(" − Kaynak "), on_click(self.project_panel.remove_source)),
                (
                    "class:button",
                    self.tr(" Sil "),
                    on_click(
                        lambda: self.delete_record("container", self.current_project.get("id"))
                    ),
                ),
                (
                    "class:button",
                    self.tr(" ↻ Eşitle "),
                    on_click(self.show_project_details),
                ),
            ]
        )

    def render_chat_header(self):
        if self.tab_kind == "settings":
            return FormattedText([("class:settings.title", self.tr(" Ayarlar"))])
        if self.editing_project():
            return FormattedText([])
        selected = self.catalog.chat_model(self.model)
        model = (
            (selected.chat_title or selected.title)
            if selected
            else self.model
            or (self.tr("Yükleniyor…") if self.catalog_loading else self.tr("Seçilmedi"))
        )
        effort = (
            next(
                (choice.title for choice in selected.efforts if choice.id == self.effort),
                self.effort or self.tr("Sağlayıcı varsayılanı"),
            )
            if selected
            else self.effort or self.tr("Sağlayıcı varsayılanı")
        )
        if selected and not selected.efforts:
            effort = (
                self.tr("Okunamadı · varsayılan")
                if selected.metadata.get("effort_error")
                else self.tr("Desteklenmiyor")
            )
        model_text = self.tr(" Model: {0} ▾ ").format(model)
        effort_text = self.tr(" Efor: {0} ▾ ").format(effort)
        project_text = (
            self.tr(" Proje: ")
            + fit_text(self.current_project.get("name") or self.tr("Projesiz"), 22)
            + " ▾ "
        )
        temporary_supported = Capability.TEMPORARY in self.provider_features
        temporary_text = (
            self.tr(" Geçici: ")
            + (
                (self.tr("Açık ●") if self.temporary else self.tr("Kapalı ○"))
                if temporary_supported
                else self.tr("Desteklenmiyor")
            )
            + " "
        )
        fragments = (
            list(self.render_project_actions()) + [("", "\n")] if self.tab_kind == "project" else []
        )
        used = 0
        width = self.terminal_width()
        for style, label, callback in (
            ("class:metadata.model", model_text, lambda: self.handle_command("/model")),
            ("class:metadata.effort", effort_text, lambda: self.handle_command("/effort")),
            ("class:metadata.project", project_text, self.open_project_picker),
            ("class:metadata.temporary", temporary_text, self.toggle_temporary_chat),
        ):
            if self.tab_kind == "project" and style in (
                "class:metadata.project",
                "class:metadata.temporary",
            ):
                continue
            if (
                style == "class:metadata.project"
                and Capability.PROJECTS not in self.provider_features
            ):
                continue
            label = fit_text(label, width)
            if used:
                separator = " · " if used + 3 + get_cwidth(label) <= width else "\n"
                fragments.append(("class:help", separator))
                used = 0 if separator == "\n" else used + 3
            fragments.append((style, label, on_click(callback)))
            used += get_cwidth(label)
        return FormattedText(fragments)

    def toggle_temporary_chat(self):
        if not self.temporary and Capability.TEMPORARY not in self.provider_features:
            self.status = self.tr("Bu sağlayıcı geçici sohbeti desteklemiyor")
        elif self.busy or self.loading_conversation or self.background_jobs:
            self.status = self.tr("Devam eden işlem tamamlandıktan sonra geçici modu değiştirin")
        elif self.conversation_id or self.messages:
            self.status = self.tr(
                "Geçici mod yalnız ilk mesajdan önce değiştirilebilir · yeni sohbet açın"
            )
        else:
            self.temporary = not self.temporary
            if self.temporary:
                self.current_project = {}
            self.title = self.tr("Geçici sohbet") if self.temporary else self.tr("Yeni sohbet")
            self.status = (
                self.tr("Geçici sohbet · yerel kayıt kapalı")
                if self.temporary
                else self.tr("Normal sohbet")
            )
            self._save_active_tab()
        self.app.invalidate()

    def new_temporary_chat(self):
        if self.account_service and Capability.TEMPORARY not in self.provider_features:
            self.status = self.tr("Bu sağlayıcı geçici sohbeti desteklemiyor")
            self.app.invalidate()
            return
        count = len(self.tabs)
        self.new_tab()
        if len(self.tabs) == count:
            return
        self.temporary = True
        self.current_project = {}
        self.title = self.tr("Geçici sohbet")
        self.status = self.tr("Geçici sohbet · yerel kayıt kapalı")
        self._save_active_tab()
        self.app.invalidate()

    def open_project_picker(self, purpose="select"):
        self.project_picker_purpose = purpose
        query = (
            self.palette_search.text
            if self.palette_visible and self.palette_mode == "projects"
            else ""
        )
        self.open_palette("projects")
        self.palette_items = [
            {
                "id": "project.select:",
                "title": self.tr("Projesiz sohbet"),
                "detail": self.tr("Projeden çıkar / projesiz başlat"),
            }
        ] + [
            {
                "id": (
                    "project." + purpose + ":"
                    if purpose in ("edit", "delete")
                    else "project.select:"
                )
                + project_id,
                "title": item.get("name") or project_id,
                "detail": origin(project_id, self.account_key)[0]
                if purpose in ("edit", "delete")
                else self.tr("Sohbeti taşı / yeni sohbete bağla"),
            }
            for project_id, item in self.project_items.items()
            if purpose in ("edit", "delete")
            or origin(project_id, self.account_key)[0] == self.account_key
        ]
        self.palette_items.append(
            {
                "id": "command.temporary",
                "title": self.tr("Yeni geçici sohbet"),
                "detail": self.tr("Yerel kayıt yok"),
            }
        )
        if self.project_cursor:
            self.palette_items.append(
                {"id": "project.more", "title": self.tr("Daha fazla proje…"), "detail": ""}
            )
        if purpose in ("edit", "delete"):
            self.palette_items = [
                i
                for i in self.palette_items
                if i["id"].startswith("project." + purpose + ":") or i["id"] == "project.more"
            ]
        self.palette_search.text = query
        self.filter_palette()
        if not self.projects_loaded and not self.projects_loading:
            self.refresh_projects()

    def choose_project(self, project_id):
        if project_id and project_id not in self.project_items:
            self.status = self.tr("Listeden erişilebilir bir proje seçin")
            self.app.invalidate()
            return
        if self.temporary:
            self.status = self.tr("Geçici sohbet bir projeye bağlanamaz")
            self.app.invalidate()
            return
        if self.busy or self.loading_conversation or self.background_jobs:
            self.status = self.tr("Devam eden işlem tamamlandıktan sonra proje değiştirin")
            self.app.invalidate()
            return
        if self.tab_kind != "chat":
            self.status = self.tr("Önce bir sohbet sekmesi seçin")
            self.app.invalidate()
            return
        project = dict(self.project_items[project_id]) if project_id else {}
        if project_id and origin(project_id, self.account_key)[0] != self.account_key:
            self.status = self.tr(
                "Proje başka bir sağlayıcı hesabına ait; önce o hesapta sohbet açın"
            )
            self.app.invalidate()
            return
        if project.get("metadata", {}).get("read_only") and not (
            project.get("metadata", {}).get("supports_chat")
            or (self.provider_id == "gemini" and project.get("kind") == "notebook")
        ):
            self.status = self.tr("Gemini defteri şu anda salt okunur · F9 ile ayrıntıları açın")
            self.app.invalidate()
            return
        if not self.conversation_id:
            self.current_project = project
            self._save_active_tab()
            self.status = self.tr("Yeni sohbet · ") + (project.get("name") or self.tr("Projesiz"))
            self.app.invalidate()
            return
        if project_id == (self.current_project or {}).get("id"):
            self.status = (
                self.tr("Sohbet zaten bu projede")
                if project_id
                else self.tr("Sohbet zaten projesiz")
            )
            self.app.invalidate()
            return
        if self.move_conversation is None:
            self.status = self.tr("Sağlayıcı bu sohbeti taşımayı desteklemiyor")
            self.app.invalidate()
            return
        remote_id = self.conversation_id
        target_tab = self.tabs[self.active_tab]
        target_tab["project_changing"] = True
        move = self.move_conversation
        self.background_jobs += 1
        self.status = self.tr("Proje değiştiriliyor…")
        self.app.invalidate()

        def completed(_result):
            self.background_jobs -= 1
            target_tab["project_changing"] = False
            for tab in self.tabs:
                if tab.get("conversation_id") == remote_id:
                    tab["project"] = dict(project)
            if self.conversation_id == remote_id:
                self.current_project = dict(project)
                self._save_active_tab()
            for item in self.sidebar_items:
                if item.get("id") == remote_id:
                    item.update(
                        project_id=project_id or None,
                        project_name=project.get("name"),
                        project=dict(project),
                    )
            self.filter_sidebar()
            self.status = self.tr("Sohbet · ") + (project.get("name") or self.tr("Projesiz"))
            self.app.invalidate()

        def failed(reason):
            self.background_jobs -= 1
            target_tab["project_changing"] = False
            self.status = self.tr("Proje değiştirilemedi: ") + str(reason)
            self.app.invalidate()

        self._read_in_background(
            lambda: move(remote_id, project_id or None), completed, failed, session_bound=False
        )

    def scroll_transcript(self, direction):
        self.pause_following()
        self.transcript_window.vertical_scroll = max(
            0, self.transcript_window.vertical_scroll + direction * self.transcript_page_size()
        )
        self.app.invalidate()

    def scroll_top(self):
        if self.history_pager.request("top"):
            return
        self.pause_following()
        self.transcript_window.vertical_scroll = 0
        self.app.invalidate()

    def render_composer_actions(self):
        compact = self.main_width() < 42
        controls = [
            (
                "class:button" if self.conversation_id else "class:sidebar.muted",
                " × " if compact else self.tr(" Sil "),
                on_click(
                    lambda: (
                        self.delete_record(value=self.conversation_id)
                        if self.conversation_id
                        else None
                    )
                ),
            )
        ]
        if self.attach_file_callback or self.pending_attachments:
            controls.append(
                (
                    "class:button",
                    f" {len(self.pending_attachments)} 📎 "
                    if self.pending_attachments
                    else " ＋ "
                    if compact
                    else self.tr(" ＋ Dosya "),
                    on_click(self.open_attachment),
                )
            )
        if self.layout_enabled:
            controls.append(
                (
                    "class:button",
                    " ◫ " if compact else self.tr(" ◫ Böl "),
                    on_click(self.begin_split),
                )
            )
        if not self.follow_output:
            controls.append(("class:button", " ↓ ", on_click(self.scroll_bottom)))
        if self.active_busy():
            controls.append(
                (
                    "class:button",
                    " ■ " if compact else " ■ " + self.tr("Durdur") + " ",
                    on_click(self.stop_active_response),
                )
            )
        else:
            controls.append(
                (
                    "class:button",
                    " ↑ " if compact else self.tr(" Gönder ↑ "),
                    on_click(self.submit_prompt),
                )
            )
        used = sum(get_cwidth(f[1]) for f in controls)
        return FormattedText([("", " " * max(0, self.main_width() - used))] + controls)

    def render_footer(self):
        controls = [
            (
                self.tr(" {0} Yardım ").format(self.shortcut("command.help")),
                lambda: self.handle_command("/help"),
            ),
            (self.downloads_panel.footer_label(), self.downloads_panel.open),
            (" ⚙ " + self.text("settings", self.tr("Ayarlar")) + " ", self.settings_dialog.open),
            (
                self.tr(" ● Hesaplar ")
                if self.session_state.get("authenticated")
                else self.tr(" ○ Hesaplar "),
                self.session_dialog.open,
            ),
        ]
        available = max(0, self.terminal_width() - sum(get_cwidth(label) for label, _ in controls))
        activity = "".join(fragment[1] for fragment in self.render_activity())
        status = fit_text(activity or " " + self.status, available)
        status += " " * max(0, available - get_cwidth(status))
        return FormattedText(
            [("class:help", status)]
            + [
                (
                    (
                        "class:connection.connected"
                        if self.session_state.get("authenticated")
                        else "class:connection.disconnected"
                    )
                    if callback == self.session_dialog.open
                    else "class:button",
                    label,
                    on_click(callback),
                )
                for label, callback in controls
            ]
        )

    def settings_rows(self):
        if self.settings_rows_callback is None:
            raise ValueError(self.tr("Ayar bağlantısı kurulmamış"))
        rows = self.settings_rows_callback()
        for row in rows:
            if row["section"] == "application" and row["key"] == "model":
                row["choices"] = ("", *(m.id for m in self.catalog.chat_models))
                row["choice_labels"] = {
                    m.id: m.chat_title or m.title for m in self.catalog.chat_models
                }
            if row["section"] == "application" and row["key"] == "effort":
                selected = self.catalog.chat_model(self.model)
                row["choices"] = tuple(e.id for e in selected.efforts) if selected else ()
                row["choice_labels"] = {e.id: e.title for e in selected.efforts} if selected else {}
        return rows

    def apply_preferences(self, preferences):
        self._invalidate_sidebar_rows()
        config = preferences.get("settings", preferences)
        application = config.get("application", {})
        self.show_reasoning = application.get("show_reasoning", True)
        self.mouse_enabled = application.get("mouse", True)
        self.layout_enabled = config.get("extensions", {}).get("layout", {}).get("enabled", True)
        if self.layout_enabled and self.layout_controller is None and self.layout_factory:
            self.layout_controller = self.layout_factory(self)
        self.translations = preferences.get("translations", self.translations)
        self.resume_search.prompt = self.tr("Search: ")
        self.sidebar_search.prompt = self.tr(" Ara ")
        self.settings_dialog.search.prompt = self.tr(" Ara  ")
        self.downloads_panel.labels()
        self.keybindings = dict(DEFAULT_KEYBINDINGS) | config.get("keybindings", {})
        self.bindings = key_bindings(self)
        remembered = (
            self.account_service.preference(self.account_key)
            if self.account_service and hasattr(self.account_service, "preference")
            else {}
        )
        self.effort_preference = (
            (remembered.get("effort") or "")
            if remembered
            else application.get("effort", self.effort_preference)
        )
        previous = getattr(self, "application_preferences", {})
        if not previous and not self.conversation_id and self.title == "New conversation":
            self.title = self.tr("New conversation")
        self.application_preferences = dict(application)
        self.default_model_preference = application.get("model", "")
        requested = (
            application.get("model") if previous.get("model") != application.get("model") else None
        )
        if (
            not remembered
            and requested
            and any(m.id == requested for m in self.catalog.chat_models)
        ):
            self.select_model(requested)
        if (
            not remembered
            and previous.get("effort") != application.get("effort")
            and not self.tabs[self.active_tab].get("busy")
        ):
            self.effort = self.catalog.effort(self.model, self.effort_preference)
            self._save_active_tab()
        tokens = preferences.get("tokens", {})
        render_labels = {
            "reasoning_label": self.tr("Düşünme özeti"),
            "waiting_label": self.tr("Yanıt hazırlanıyor…"),
        }
        try:
            self.app.style = terminal_style(tokens)
            self.renderer.configure(dict(tokens, **render_labels))
            self.theme_options = tokens.get("options", {})
            self._transcript_signature = None
        except Exception:
            self.app.style = Style.from_dict(DEFAULT_TERMINAL_STYLE)
            self.renderer.configure(render_labels)
            self.theme_options = {}
            self.status = self.tr("Tema stilleri geçersiz; varsayılan görünüm kullanılıyor")
        if (
            self.account_service
            and self.account_key
            and hasattr(self.account_service, "enabled")
            and not self.account_service.enabled(self.provider_id)
        ):
            if self.tab_kind == "chat":
                self.select_account(self.account_service.active())
            else:
                self.bind_account(self.account_service.active())
                self.restore_account_preference()
                self._save_active_tab()
            self.refresh_sidebar()
        self.settings_dialog.save_button.text = self.text("save", self.tr("Kaydet"))
        self.settings_dialog.cancel_button.text = self.text("close", self.tr("Kapat"))
        self.app.invalidate()

    def after_logout(self):
        retained_tabs = (
            [
                tab
                for tab in self.tabs
                if tab.get("account_key", self.account_key) != self.account_key
            ]
            if self.account_service
            else []
        )
        self.session_epoch += 1
        self._account_catalogs.pop(self.account_key, None)
        self._catalog_requests.pop(self.account_key, None)
        self._project_requests.pop(self.account_key, None)
        self.sidebar_refresh_generation += 1
        self.sidebar_items.clear()
        self.project_items.clear()
        self.projects_loaded = False
        self.catalog = ModelCatalog(FeatureState.UNKNOWN)
        self.catalog_loading = self.sidebar_loading = self.projects_loading = False
        self.project_chats_loading.clear()
        self.project_chats_loaded.clear()
        self.project_chat_cursors.clear()
        self.project_chat_errors.clear()
        self.history_cursor = self.project_cursor = None
        self.history_loading = False
        render_tokens = dict(self.renderer.tokens)
        self.renderer = MessageRenderer()
        self.renderer.configure(render_tokens)
        self.message_selection = False
        self.selected_message = 0
        self.selected_message_ref = None
        self.message_ranges = []
        self._transcript_signature = None
        self._transcript_cache = ""
        self.resume_items = self.resume_filtered = []
        self.resume_loading = False
        self.sidebar_rows = []
        self._invalidate_sidebar_rows()
        self.conversation_id = None
        self.title = self.tr("New conversation")
        self.messages = []
        self.current_project = {}
        self.temporary = False
        self._release_staged_attachments(self.pending_attachments)
        self.pending_attachments = []
        self.input.text = ""
        settings_open = self.settings_dialog.visible
        self.tab_kind = "settings" if settings_open else "chat"
        self.title = self.tr("Ayarlar") if settings_open else self.tr("New conversation")
        self.tabs = retained_tabs + [dict(self._new_tab_state(), tab_id=uuid.uuid4().hex)]
        self.active_tab = len(self.tabs) - 1
        self.session_state = {"authenticated": False, "saved_snapshot": False}
        self.status = self.tr("Oturum kapatıldı · F12 ile giriş yapın")
        self.app.invalidate()

    def shortcut(self, action_id: str) -> str:
        value = self.keybindings.get(action_id, "")
        names = {
            "c-": "Ctrl+",
            "s-": "Shift+",
            "escape left": "Alt+←",
            "escape right": "Alt+→",
        }
        if value in names:
            return names[value]
        if value.startswith("c-"):
            return f"Ctrl+{value[2:].upper()}"
        if value.startswith("s-"):
            return f"Shift+{value[2:].upper()}"
        if value.startswith("f") and value[1:].isdigit():
            return value.upper()
        return value.replace(" ", "+").title()

    def _sidebar_sync_states(self):
        archive = self.archive_service()
        if not archive:
            return {}
        result = {}
        for item in self.sync_states(archive):
            error = str(item.get("error") or "").casefold()
            if (
                item["kind"] == "container"
                and item["state"] == "error"
                and any(
                    marker in error
                    for marker in (
                        "kayıtlı oturum yok",
                        "http 401",
                        "unauthorized",
                        "authentication required",
                    )
                )
            ):
                # Authentication is account-wide. Repeating the same ! on every
                # cached project suggests independent project failures.
                continue
            result[item["account"], item["kind"], item["remote"]] = item["state"]
        return result

    def render_sidebar_row(self, index, states=None, open_ids=None):
        if not 0 <= index < len(self.sidebar_rows):
            return []
        row = self.sidebar_rows[index]
        kind = row["kind"]
        if kind == "message":
            return [
                (
                    "class:sidebar.muted",
                    "   " + fit_text(row["title"], self.sidebar_width() - 3) + "\n",
                )
            ]
        states = states if states is not None else {}
        if open_ids is None:
            open_ids = {tab.get("conversation_id") for tab in self.tabs}
        selected = index == self.sidebar_index
        click = on_click(lambda i=index: self.click_sidebar(i))
        style = "class:sidebar.selected" if selected else "class:sidebar.item"
        if row.get("id") in open_ids:
            style += " class:sidebar.open"
        marker = (
            ("▸" if row["collapsed"] else "▾")
            if kind in ("section", "project")
            else ("›" if selected else " ")
        )
        indent = "   " if row.get("nested") else " "
        fragments = [(style, indent + marker + " ", click)]
        if kind in ("chat", "project"):
            provider = row.get("provider_id", "chatgpt")
            fragments.append(
                (
                    "class:provider." + provider,
                    PROVIDERS.get(provider, (provider, "◈", ""))[1] + " ",
                    click,
                )
            )
        suffix = " " + str(row.get("count", "")) if kind in ("section", "project") else ""
        if kind == "project":
            suffix += " ⓘ"
        section_action = None
        if kind == "section" and row.get("id") in ("projects", "chats"):
            section_action = self.open_project_create if row["id"] == "projects" else self.new_tab
        key, remote = origin(row.get("id"), self.account_key)
        state = states.get((key, "conversation" if kind == "chat" else "container", remote))
        live_tab = row.get("live_tab")
        if kind == "chat" and live_tab is not None and live_tab.get("busy"):
            suffix += " ●"
            style += " class:sidebar.running"
        elif kind == "chat" and live_tab is not None and live_tab.get("completed_pending"):
            suffix += " ✓"
        if state == "syncing":
            suffix += " ↻"
        elif state == "error":
            suffix += " !"
        reserve_suffix = suffix + ("  +" if section_action is not None else "")
        width = (
            self.sidebar_width()
            - len(indent)
            - 3
            - (2 if kind in ("chat", "project") else 0)
            - get_cwidth(reserve_suffix)
        )
        title = fit_text(row["title"], max(1, width))
        fragments.append(
            (
                style + (" bold" if kind == "section" else ""),
                title,
                click,
            )
        )
        if kind == "project":
            fragments.append(
                (
                    "class:sidebar.muted",
                    suffix + "\n",
                    on_click(lambda project_id=row["id"]: self.show_project_details(project_id)),
                )
            )
        elif section_action is not None:
            padding = max(0, width - get_cwidth(title))
            if padding:
                fragments.append((style, " " * padding, click))
            fragments.append((style, suffix, click))
            fragments.append((style, "  +", on_click(section_action)))
            fragments.append((style, "\n", click))
        else:
            fragments.append((style, suffix + "\n", click))
        return fragments

    def render_sidebar(self) -> FormattedText:
        self._build_sidebar_rows()
        states = self._sidebar_sync_states()
        open_ids = {tab.get("conversation_id") for tab in self.tabs}
        fragments = []
        for index in range(len(self.sidebar_rows)):
            fragments.extend(self.render_sidebar_row(index, states, open_ids))
        return FormattedText(fragments)

    def click_sidebar(self, index):
        self.sidebar_index = index
        self.activate_sidebar_item()

    def click_palette(self, index):
        self.palette_index = index
        self.execute_palette_item()

    def _invalidate_sidebar_rows(self):
        self._sidebar_rows_dirty = True

    def _build_sidebar_rows(self, force=True):
        if force or self._sidebar_rows_dirty:
            build_sidebar_rows(self)
            self._sidebar_rows_dirty = False
        return self.sidebar_rows

    def _chat_row(self, item: dict[str, Any], nested: bool = False) -> dict[str, Any]:
        return {
            "kind": "chat",
            "id": item.get("id"),
            "project_id": item.get("project_id"),
            "title": str(item.get("title") or self.tr("Adsız sohbet")),
            "provider_id": item.get("provider_id") or origin(item.get("id"))[0].split("/")[0],
            "nested": nested,
            "live_tab": item.get("live_tab"),
        }

    def toggle_sidebar(self) -> None:
        self.close_overlays()
        self.sidebar_visible = not self.sidebar_visible
        self.app.layout.focus(self.sidebar_control if self.sidebar_visible else self.input)
        self.app.invalidate()

    def focus_sidebar(self) -> None:
        self.close_overlays()
        if not self.sidebar_visible:
            self.sidebar_visible = True
        self.app.layout.focus(self.sidebar_control)
        self.status = self.tr("Sohbetler")
        self.app.invalidate()

    def toggle_sidebar_group(self) -> None:
        self._build_sidebar_rows()
        if not self.sidebar_rows:
            return
        row = self.sidebar_rows[self.sidebar_index]
        if row["kind"] not in ("section", "project"):
            return
        key = f"project:{row['id']}" if row["kind"] == "project" else f"section:{row['id']}"
        if key in self.sidebar_collapsed:
            self.sidebar_collapsed.remove(key)
            if row["kind"] == "project" and row["id"] not in self.project_chats_loaded:
                self.load_project_chats(row["id"])
        else:
            self.sidebar_collapsed.add(key)
        self._build_sidebar_rows()
        self.ensure_sidebar_selection_visible()
        self.app.invalidate()

    def ensure_sidebar_selection_visible(self) -> None:
        info = self.sidebar_window.render_info
        height = (
            info.window_height if info is not None else max(1, self.viewport_height().preferred - 5)
        )
        first = self.sidebar_window.vertical_scroll
        if self.sidebar_index < first:
            self.sidebar_window.vertical_scroll = self.sidebar_index
        elif self.sidebar_index >= first + height:
            self.sidebar_window.vertical_scroll = self.sidebar_index - height + 1

    def load_more_history(self):
        if not self.list_history_page or not self.history_cursor or self.history_loading:
            return
        self.history_loading = True
        self._invalidate_sidebar_rows()
        self.app.invalidate()
        cursor = self.history_cursor

        def loaded(page):
            by_id = {item["id"]: item for item in self.sidebar_items if item.get("id")}
            for item in page["items"]:
                if item.get("id"):
                    by_id[item["id"]] = merge_chat_listing(by_id.get(item["id"]), item)
            self.sidebar_items = list(by_id.values())
            self.history_cursor = page.get("cursor") if page.get("cursor") != cursor else None
            self.history_loading = False
            self.filter_sidebar()

        def failed(reason):
            self.history_loading = False
            self._invalidate_sidebar_rows()
            self.status = str(reason)
            self.app.invalidate()

        self._read_in_background(lambda: self.list_history_page(cursor), loaded, failed)

    def refresh_sidebar(self) -> None:
        self.refresh_projects()
        if self.sidebar_loading:
            self.sidebar_refresh_pending = True
            return
        self.sidebar_refresh_generation += 1
        generation = self.sidebar_refresh_generation
        self.sidebar_loading = True
        self.app.invalidate()

        def load() -> None:
            conversations: list[dict[str, Any]] = []
            cursor = None
            workspaces: list[dict[str, Any]] = []
            sidebar_error = None
            partial_error = None
            workspace_error = None
            try:
                page = self.list_history_page(None) if self.list_history_page else None
                conversations = [
                    item
                    for item in (page["items"] if page is not None else self.list_conversations())
                    if isinstance(item, dict)
                ]
                cursor = page.get("cursor") if page is not None else None
                if page and page.get("errors"):
                    partial_error = self.tr("Yenilenemeyen hesaplar: ") + ", ".join(page["errors"])
                    failed = set(page.get("failed_accounts", []))
                    conversations.extend(
                        item
                        for item in self.sidebar_items
                        if origin(item.get("id"), self.account_key)[0] in failed
                    )
            except Exception as error:
                sidebar_error = str(error)
            try:
                workspaces = [item for item in self.list_workspaces() if isinstance(item, dict)]
            except Exception as error:
                workspace_error = str(error)

            def apply_results() -> None:
                if generation != self.sidebar_refresh_generation:
                    return
                # Keep the last good list on transient errors instead of
                # blanking the navigation tree while a refresh is in flight.
                if sidebar_error is None:
                    project_chats = [
                        item
                        for item in self.sidebar_items
                        if item.get("project_id") in self.project_chats_loaded
                    ]
                    by_id = {item["id"]: item for item in project_chats if item.get("id")}
                    for item in conversations:
                        if item.get("id"):
                            by_id[item["id"]] = merge_chat_listing(by_id.get(item["id"]), item)
                    chats = list(by_id.values())
                else:
                    chats = [item for item in self.sidebar_items if item.get("kind") != "workspace"]
                saved_workspaces = (
                    [dict(item, kind="workspace") for item in workspaces]
                    if workspace_error is None
                    else [item for item in self.sidebar_items if item.get("kind") == "workspace"]
                )
                if sidebar_error is None:
                    self.history_cursor = cursor
                self.sidebar_items = chats + saved_workspaces
                self.sidebar_error = sidebar_error or partial_error
                self.workspace_error = workspace_error
                self.sidebar_loading = False
                if sidebar_error or workspace_error or partial_error:
                    self.status = partial_error or self.tr(
                        "Sol panel yenilenemedi; son kayıtlar korunuyor"
                    )
                self.filter_sidebar()
                if getattr(self, "sidebar_refresh_pending", False):
                    self.sidebar_refresh_pending = False
                    self.refresh_sidebar()

            self._dispatch_ui(apply_results)

        if not self._reads.submit(load):
            self.sidebar_loading = False
            self.status = self.tr("Arka plan iş kuyruğu dolu; yeniden deneyin")

    def _read_in_background(self, read, success, failure, session_bound=True, *, cpu=False) -> None:
        if self.event_loop is None:
            self.event_loop = getattr(self.app, "loop", None)
        epoch = self.session_epoch
        original_success, original_failure = success, failure

        def success(result):
            if not session_bound or epoch == self.session_epoch:
                original_success(result)

        def failure(reason):
            if not session_bound or epoch == self.session_epoch:
                original_failure(reason)

        def work():
            try:
                result = read()
            except Exception as error:
                reason = str(error)
                self._dispatch_ui(lambda: failure(reason))
            else:

                def deliver():
                    try:
                        success(result)
                    except Exception as error:
                        failure(str(error))

                self._dispatch_ui(deliver)

        pool = self._render_reads if cpu else self._reads
        if not pool.submit(work):
            self._dispatch_ui(
                lambda: failure(self.tr("Arka plan iş kuyruğu dolu; yeniden deneyin"))
            )

    def refresh_provider_catalog(self, force=False) -> None:
        key = self.account_key
        if self.load_catalog is None or key in self._catalog_requests:
            self.catalog_loading = key in self._catalog_requests
            return
        token = uuid.uuid4().hex
        self._catalog_requests[key] = token
        self.catalog_loading = True
        epoch = self.session_epoch
        callback = self.load_catalog
        if self.account_service and hasattr(self.account_service, "models"):

            def callback():
                return self.account_service.models(key, force=force)

        def finish():
            if self._catalog_requests.get(key) == token:
                self._catalog_requests.pop(key, None)
            self.catalog_loading = self.account_key in self._catalog_requests

        def success(catalog):
            finish()
            if epoch != self.session_epoch:
                return
            if not isinstance(catalog, ModelCatalog):
                failure(self.tr("Sağlayıcı model kataloğu geçersiz"))
                return
            self._account_catalogs[key] = catalog
            if self.account_key == key:
                self._save_active_tab()
            for tab in self.tabs:
                if tab.get("account_key") != key:
                    continue
                tab["catalog"] = catalog
                if tab.get("busy"):
                    continue
                selected = catalog.chat_model(tab.get("model"))
                if selected is None and catalog.default_chat_model is not None:
                    tab["response_model"] = tab.get("response_model") or tab.get("model")
                    selected = catalog.default_chat_model
                    tab["model"] = selected.id
                if selected and tab.get("effort") not in {e.id for e in selected.efforts}:
                    preferred = tab.get("effort_preference", self.effort_preference)
                    tab["effort"] = catalog.effort(selected.id, preferred) if preferred else None
            if self.account_key != key:
                self.app.invalidate()
                return
            self.catalog = catalog
            self.catalog_error = catalog.reason
            # Cached model availability is not proof of a connected account.
            if self.account_service:
                self.session_state = self.account_service.status(key)
            tab = self.tabs[self.active_tab]
            self.model = tab.get("model") or ""
            self.effort = tab.get("effort")
            self.last_response_model = tab.get("response_model")
            if not self.active_busy():
                self.status = self.tr("{0} model · hazır").format(len(catalog.chat_models))
            if self.palette_visible and self.palette_mode in ("models", "effort"):
                query = self.palette_search.text
                self.open_palette(self.palette_mode)
                self.palette_search.text = query
            self.app.invalidate()

        def failure(reason):
            finish()
            if epoch == self.session_epoch and self.account_key == key:
                self.catalog_error = reason
                self.status = self.tr("Model kataloğu alınamadı: {0} · F10 yeniden dene").format(
                    reason
                )
            self.app.invalidate()

        self._read_in_background(callback, success, failure, session_bound=False)

    def normalize_model_selection(self):
        if self.catalog.state != FeatureState.AVAILABLE or self.active_busy():
            return
        selected = self.catalog.chat_model(self.model)
        if selected is None:
            preferred = getattr(self, "default_model_preference", "")
            selected = next(
                (m for m in self.catalog.chat_models if m.id == preferred),
                self.catalog.default_chat_model,
            )
            if selected:
                if self.model:
                    self.last_response_model = self.model
                self.model = selected.id
        if selected and self.effort not in {choice.id for choice in selected.efforts}:
            self.effort = (
                self.catalog.effort(selected.id, self.effort_preference)
                if self.effort_preference
                else None
            )

    def select_model(self, model_id: str) -> None:
        if self.tabs[self.active_tab].get("busy"):
            self.status = self.tr("Yanıt tamamlanınca bu sohbetin modelini değiştirebilirsiniz")
            self.app.invalidate()
            return
        selected = self.catalog.chat_model(model_id)
        if selected is None:
            self.status = self.tr(
                "Bu model normal sohbet kataloğunda yok · F10 yenile · /model ile seçin"
            )
        else:
            self.model = selected.id
            self.effort = (
                self.catalog.effort(selected.id, self.effort_preference)
                if self.effort_preference
                else None
            )
            self.remember_selection()
            self._save_active_tab()
            self.status = self.tr("Model: {0} · efor: {1}").format(
                selected.chat_title or selected.title,
                self.effort or self.tr("sağlayıcı varsayılanı"),
            )
        self.app.invalidate()

    def select_effort(self, effort: str | None) -> None:
        if self.tabs[self.active_tab].get("busy"):
            self.status = self.tr("Yanıt tamamlanınca bu sohbetin eforunu değiştirebilirsiniz")
            self.app.invalidate()
            return
        selected = self.catalog.chat_model(self.model)
        if effort is not None and (
            selected is None or effort not in {e.id for e in selected.efforts}
        ):
            self.status = self.tr(
                "Bu efor seçili modelin kataloğunda yok · /effort ile seçenekleri görün"
            )
        else:
            self.effort = effort
            self.effort_preference = effort or ""
            self.remember_selection()
            self._save_active_tab()
            self.status = self.tr("Efor: {0}").format(effort or self.tr("sağlayıcı varsayılanı"))
        self.app.invalidate()

    def refresh_projects(self, more: bool = False) -> None:
        if Capability.PROJECTS not in self.provider_features and not (
            self.account_service and hasattr(self.account_service, "all_projects")
        ):
            self.projects_loading = False
            self.projects_loaded = True
            self._invalidate_sidebar_rows()
            return
        account_key = self.account_key
        if self.list_projects_callback is None or account_key in self._project_requests:
            return
        cursor = self.project_cursor if more else None
        token = uuid.uuid4().hex
        self._project_requests[account_key] = token
        self.projects_loading = True
        self._invalidate_sidebar_rows()
        self.app.invalidate()
        callback = self.list_projects_callback
        epoch = self.session_epoch
        project_scope = tuple(sorted(self.account_filter or ()))
        aggregate_accounts = bool(
            self.account_service and hasattr(self.account_service, "all_projects")
        )

        def request_is_current():
            return (
                self._project_requests.get(account_key) == token
                and epoch == self.session_epoch
                and tuple(sorted(self.account_filter or ())) == project_scope
                and (aggregate_accounts or self.account_key == account_key)
            )

        def apply_project_page(account, page):
            def apply():
                if not request_is_current() or isinstance(page, BaseException):
                    return
                if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                    return
                projects = (
                    dict(self.project_items)
                    if more
                    else {
                        key: item
                        for key, item in self.project_items.items()
                        if origin(key, account["key"])[0] != account["key"]
                    }
                )
                for item in page["items"]:
                    if not isinstance(item, dict) or not item.get("id"):
                        continue
                    projects[item["id"]] = dict(item, name=item.get("name") or self.tr("Proje"))
                self.project_items = projects
                self.projects_loaded = True
                for tab in self.tabs:
                    project = tab.get("project") or {}
                    if project.get("id") in projects:
                        project["name"] = projects[project["id"]]["name"]
                        tab["project"] = project
                self.current_project = dict(self.tabs[self.active_tab].get("project") or {})
                self.filter_sidebar()
                if self.palette_visible and self.palette_mode == "projects":
                    self.open_project_picker(getattr(self, "project_picker_purpose", "select"))
                self.app.invalidate()

            self._dispatch_ui(apply)

        if self.account_service and hasattr(self.account_service, "all_projects"):

            def callback(cursor):
                return self.account_service.all_projects(
                    cursor,
                    account_keys=self.account_filter,
                    on_account=apply_project_page,
                )

        def finish():
            if self._project_requests.get(account_key) == token:
                self._project_requests.pop(account_key, None)
            self.projects_loading = self.account_key in self._project_requests
            self._invalidate_sidebar_rows()

        def success(result):
            current = request_is_current()
            finish()
            if not current:
                return
            if not isinstance(result, dict) or not isinstance(result.get("items"), list):
                failure(self.tr("Proje listesi geçersiz"), finished=True)
                return
            next_cursor = result.get("cursor")
            if cursor and next_cursor == cursor:
                failure(self.tr("Sağlayıcı aynı proje sayfasını döndürdü"), finished=True)
                return
            failed = set(result.get("failed_accounts", []))
            projects = (
                dict(self.project_items)
                if more
                else {
                    key: item
                    for key, item in self.project_items.items()
                    if origin(key, self.account_key)[0] in failed
                }
            )
            for item in result["items"]:
                if not isinstance(item, dict) or not item.get("id"):
                    failure(self.tr("Proje kimliği eksik"), finished=True)
                    return
                projects[item["id"]] = dict(item, name=item.get("name") or self.tr("Proje"))
            self.project_items = projects
            self._save_active_tab()
            for tab in self.tabs:
                project = tab.get("project") or {}
                if project.get("id") in projects:
                    project["name"] = projects[project["id"]]["name"]
                    tab["project"] = project
            self.current_project = dict(self.tabs[self.active_tab].get("project") or {})
            self.project_cursor = next_cursor
            self.projects_loaded = True
            self.projects_loading = False
            self.project_error = (
                (self.tr("Yenilenemeyen hesaplar: ") + ", ".join(result["errors"]))
                if result.get("errors")
                else None
            )
            self.filter_sidebar()
            if self.palette_visible and self.palette_mode == "projects":
                self.open_project_picker(getattr(self, "project_picker_purpose", "select"))

        def failure(reason, *, finished=False):
            current = (
                epoch == self.session_epoch
                and tuple(sorted(self.account_filter or ())) == project_scope
                and (aggregate_accounts or self.account_key == account_key)
                and (finished or self._project_requests.get(account_key) == token)
            )
            finish()
            if not current:
                return
            self.project_error = reason
            self.status = self.tr("Proje listesi alınamadı: {0}").format(reason)
            self.filter_sidebar()

        self._read_in_background(lambda: callback(cursor), success, failure, session_bound=False)

    def load_project_chats(self, project_id: str, more: bool = False) -> None:
        if self.list_project_chats_callback is None or project_id in self.project_chats_loading:
            return
        cursor = self.project_chat_cursors.get(project_id) if more else None
        self.project_chats_loading.add(project_id)
        self._invalidate_sidebar_rows()
        self.app.invalidate()

        def success(result):
            if not isinstance(result, dict) or not isinstance(result.get("items"), list):
                failure(self.tr("Proje sohbetleri geçersiz"))
                return
            next_cursor = result.get("cursor")
            if cursor and next_cursor == cursor:
                failure(self.tr("Sağlayıcı aynı sohbet sayfasını döndürdü"))
                return
            items = {item["id"]: item for item in self.sidebar_items if item.get("id")}
            for item in result["items"]:
                if not isinstance(item, dict) or not item.get("id"):
                    failure(self.tr("Proje sohbet kimliği eksik"))
                    return
                items[item["id"]] = dict(
                    item,
                    project_id=project_id,
                    project_name=self.project_items.get(project_id, {}).get("name"),
                )
            self.sidebar_items = list(items.values())
            self.project_chats_loaded.add(project_id)
            self.project_chats_loading.discard(project_id)
            self.project_chat_errors.pop(project_id, None)
            self.project_chat_cursors[project_id] = next_cursor
            self.filter_sidebar()

        def failure(reason):
            self.project_chats_loading.discard(project_id)
            self.project_chat_errors[project_id] = reason
            self.filter_sidebar()

        self._read_in_background(
            lambda: self.list_project_chats_callback(project_id, cursor), success, failure
        )

    def project_messages(self, project):
        resources = project.get("resources") or []
        name = project.get("name") or self.tr("Proje")
        instructions = project.get("instructions") or self.tr("Talimat yok.")
        text = (
            "\n\n".join(
                [
                    "# " + name,
                    project.get("description") or "",
                    self.tr("## Talimatlar"),
                    instructions,
                    self.tr("## Kaynaklar"),
                ]
            )
            + "\n\n"
        )
        text += "\n".join(
            f"- {r.get('name') or r.get('id')} ({r.get('media_type') or r.get('type') or self.tr('kaynak')})"
            for r in resources
        ) or self.tr("Kaynak yok.")
        if project.get("requires_auth"):
            text += "\n\n" + self.tr(
                "Showing cached project details. Verify the provider account to refresh or edit them."
            )
        return [{"role": "system", "text": text}]

    def show_project_details(self, project_id: str | None = None) -> None:
        if (
            not project_id
            and self.app.layout.current_control
            in (self.sidebar_control, self.sidebar_search.control)
            and self.sidebar_rows
        ):
            row = self.sidebar_rows[self.sidebar_index]
            project_id = row.get("id") if row["kind"] == "project" else row.get("project_id")
        project_id = project_id or self.current_project.get("id")
        if not project_id:
            self.status = self.tr("Sol panelde bir proje seçip F9'a basın")
            self.app.invalidate()
            return
        if self.get_project_callback is None:
            self.status = self.tr("Sağlayıcının proje ayrıntısı bağlantısı kurulmamış")
            self.app.invalidate()
            return
        existing = next(
            (
                i
                for i, tab in enumerate(self.tabs)
                if tab.get("kind") == "project" and tab.get("project", {}).get("id") == project_id
            ),
            None,
        )
        if existing is not None:
            self._save_active_tab()
            self.active_tab = existing
            self._restore_active_tab()
        else:
            self.new_tab()
        if self.account_service:
            key, _ = origin(project_id, self.account_key)
            self.bind_account(key)
            self.restore_account_preference()
        self.tab_kind = "project"
        self.current_project = dict(self.project_items.get(project_id, {"id": project_id}))
        self.title = self.current_project.get("name") or self.tr("Proje")
        self.messages = [{"role": "system", "text": self.tr("Proje bilgileri yükleniyor…")}]
        self._save_active_tab()
        self.scroll_bottom()

        def success(project):
            self.project_items[project_id] = project
            self._invalidate_sidebar_rows()
            update(self.project_messages(project)[0]["text"], project)

        def update(text, project):
            for index, tab in enumerate(self.tabs):
                if tab.get("kind") == "project" and tab.get("project", {}).get("id") == project_id:
                    if index == self.active_tab:
                        self._save_active_tab()
                    tab.update(
                        messages=[{"role": "system", "text": text}],
                        project=project,
                        title=project.get("name") or self.tr("Proje"),
                    )
                    if index == self.active_tab:
                        self._restore_active_tab(focus=False)
                    break
            self.filter_sidebar()

        project_snapshot = dict(self.current_project)
        read_project = self.get_project_callback
        self._read_in_background(
            lambda: read_project(project_id),
            success,
            lambda reason: update(
                self.tr("Proje bilgileri alınamadı: {0}\n\nF9 ile yeniden deneyin.").format(reason),
                project_snapshot,
            ),
        )

    def _dispatch_ui(self, callback: Callable[[], None]) -> None:
        if self.account_service and getattr(self.account_service, "closed", False) is True:
            return
        loop = self.event_loop or getattr(self.app, "loop", None)
        if loop is not None:
            if loop.is_running():
                loop.call_soon_threadsafe(lambda: callback() if not self.app.is_done else None)
            return
        callback()

    def filter_sidebar(self) -> None:
        if hasattr(self, "app"):
            self._invalidate_sidebar_rows()
            self._build_sidebar_rows()
            self.ensure_sidebar_selection_visible()
            self.app.invalidate()

    def open_sidebar_search(self) -> None:
        self.close_overlays()
        self.sidebar_visible = True
        self.sidebar_search.text = ""
        self.refresh_sidebar()
        self.app.layout.focus(self.sidebar_search)
        self.app.invalidate()

    def show_projects(self) -> None:
        self.close_overlays()
        self.sidebar_visible = True
        self.sidebar_collapsed.discard("section:projects")
        self._invalidate_sidebar_rows()
        self.sidebar_index = 0
        self.sidebar_search.text = ""
        self.refresh_sidebar()
        self.app.layout.focus(self.sidebar_control)
        self.status = self.tr("Projeler")
        self.app.invalidate()

    def dispatch_action(self, action_id):
        handler = self.action_handlers.get(action_id)
        if handler is None:
            raise ValueError(self.tr("Bilinmeyen arayüz aksiyonu"))
        handler(SimpleNamespace(app=self.app))

    def run_action(self, action_id: str, arguments: dict[str, Any] | None = None) -> None:
        if action_id in self.action_handlers:
            self.dispatch_action(action_id)
            return
        if self.invoke_action is None:
            self.status = self.tr("Action {0} is unavailable").format(action_id)
            self.app.invalidate()
            return

        if arguments is None:
            description = next(
                (item for item in self.list_actions() if item["id"] == action_id), {}
            )
            fields = description.get("parameters", ())
            if fields:

                def submit(values):
                    if any(
                        field.get("required") and not str(values.get(field["key"], "")).strip()
                        for field in fields
                    ):
                        raise ValueError(self.tr("Zorunlu alanları doldurun"))
                    self.run_action(action_id, values)

                self.forms.open(
                    description.get("title", action_id),
                    [
                        (
                            field["key"],
                            field.get("label", field["key"]),
                            field.get("default", ""),
                            field.get("choices"),
                        )
                        for field in fields
                    ],
                    submit,
                )
                return
        if action_id == "layout.create" and self.layout_controller and arguments:
            arguments = dict(
                arguments, workspace=self.layout_controller.snapshot(arguments["name"])
            )
        target_tab = self.active_tab
        target_state = self.tabs[self.active_tab]
        target_messages = self.messages
        self.background_jobs += 1

        def invoke() -> None:
            preview = None
            error_text = None
            result = None
            try:
                result = self.invoke_action(action_id, arguments or {})
                if result is not None:
                    try:
                        preview = json.dumps(result, ensure_ascii=False, indent=2, default=str)
                    except (TypeError, ValueError):
                        preview = str(result)
                    if len(preview) > 4000:
                        preview = preview[:3997] + "..."
            except Exception as error:
                error_text = str(error)

            def show_result() -> None:
                self.background_jobs = max(0, self.background_jobs - 1)
                if action_id == "layout.show" and error_text is None and self.layout_controller:
                    self.layout_controller.restore(result)
                    return
                if preview is not None:
                    with self.lock:
                        target_messages.append(
                            {
                                "role": "system",
                                "text": f"**{action_id}**\n\n```text\n{preview}\n```",
                            }
                        )
                tab = target_state
                tab["messages"] = target_messages
                tab["completed_pending"] = target_state is not self.tabs[self.active_tab]
                if target_state is self.tabs[self.active_tab]:
                    self.messages = target_messages
                    self.status = (
                        self.tr("Action failed: {0}").format(error_text)
                        if error_text
                        else self.tr("Action completed: {0}").format(action_id)
                    )
                    self.scroll_bottom()
                else:
                    self.status = self.tr("Action completed in tab {0}").format(target_tab + 1)
                    self.app.invalidate()

            self._dispatch_ui(show_result)

        if not self._operations.submit(invoke):
            self.background_jobs = max(0, self.background_jobs - 1)
            self.status = self.tr("Arka plan iş kuyruğu dolu; yeniden deneyin")

    def activate_sidebar_item(self) -> None:
        self._build_sidebar_rows()
        if not self.sidebar_rows:
            return
        item = self.sidebar_rows[self.sidebar_index]
        if item["kind"] in ("section", "project"):
            self.toggle_sidebar_group()
        elif item["kind"] == "chat":
            live_tab = item.get("live_tab")
            if live_tab is not None:
                self._save_active_tab()
                index = next((i for i, tab in enumerate(self.tabs) if tab is live_tab), None)
                if index is None:
                    self.background_tabs = [
                        tab for tab in self.background_tabs if tab is not live_tab
                    ]
                    self.tabs.append(live_tab)
                    index = len(self.tabs) - 1
                self.active_tab = index
                self._restore_active_tab()
                return
            conversation_id = item.get("id")
            if isinstance(conversation_id, str) and conversation_id:
                existing_tab = next(
                    (
                        index
                        for index, tab in enumerate(self.tabs)
                        if tab.get("conversation_id") == conversation_id
                    ),
                    None,
                )
                if existing_tab is not None:
                    self._save_active_tab()
                    self.active_tab = existing_tab
                    self._restore_active_tab()
                    if self.layout_controller:
                        self.layout_controller.attach()
                    return
                upload_is_only_background_work = self.uploading_image and self.background_jobs == 1
                if self.background_jobs and not upload_is_only_background_work:
                    self.status = self.tr("Finish the active operation before loading another chat")
                    self.app.invalidate()
                    return
                if conversation_id != self.conversation_id and (
                    self.conversation_id or self.messages
                ):
                    self.new_tab()
                if self.account_service:
                    context, _ = origin(conversation_id, self.account_key)
                    if context != self.account_key:
                        self.bind_account(context)
                        self.catalog = ModelCatalog(FeatureState.UNKNOWN)
                        self.refresh_provider_catalog()
                self.resume_conversation(conversation_id)
                if self.layout_controller:
                    self.layout_controller.attach()
        elif item["kind"] == "workspace" and item.get("id"):
            self.run_action("layout.show", {"workspace_id": item["id"]})
        elif item["kind"] == "history_more":
            self.load_more_history()
        elif item["kind"] == "projects_more":
            self.refresh_projects(more=True)
        elif item["kind"] == "project_more":
            self.load_project_chats(item["id"], more=True)

    def open_palette(self, mode: str = "actions") -> None:
        self.close_overlays()
        self.palette_mode = mode
        self.palette_items = [
            {"id": key, "title": self.tr(action_title(key)), "detail": self.shortcut(key)}
            for key in sorted(self.action_handlers, key=action_order)
        ]
        if mode != "actions":
            self.palette_items = []
        if mode != "effort":
            catalogs = [(self.account_key, self.catalog)]
            if (
                self.account_service
                and hasattr(self.account_service, "active_accounts")
                and not self.conversation_id
                and not self.messages
                and self.tab_kind == "chat"
            ):
                catalogs = []
                for account in self.account_service.active_accounts():
                    key = account["key"]
                    catalog = self._account_catalogs.get(key) or self.account_service.cached_models(
                        key
                    )
                    if catalog:
                        catalogs.append((key, catalog))
                    if key not in self._model_picker_requests and (
                        catalog is None or key not in self._account_catalogs
                    ):
                        self._model_picker_requests.add(key)

                        def loaded(value, key=key):
                            self._model_picker_requests.discard(key)
                            self._account_catalogs[key] = value
                            if self.palette_visible and self.palette_mode in ("models", "actions"):
                                query = self.palette_search.text
                                self.open_palette(self.palette_mode)
                                self.palette_search.text = query

                        def failed(reason, key=key):
                            self._model_picker_requests.discard(key)
                            self.status = self.tr("Model kataloğu alınamadı: ") + key

                        self._read_in_background(
                            lambda key=key: self.account_service.models(key),
                            loaded,
                            failed,
                            session_bound=False,
                        )
            for key, catalog in catalogs:
                for model in catalog.chat_models:
                    self.palette_items.append(
                        {
                            "id": f"model.select:{model.id}"
                            if key == self.account_key
                            else f"model.account:{key}:{model.id}",
                            "title": self.tr("Model · {0}").format(model.chat_title or model.title)
                            + (" ✓" if key == self.account_key and model.id == self.model else ""),
                            "detail": key.split("/")[0] + " · " + model.id,
                        }
                    )
        selected = self.catalog.chat_model(self.model)
        if mode != "models" and selected:
            self.palette_items.extend(
                {
                    "id": f"effort.select:{choice.id}",
                    "title": self.tr("Efor · {0}").format(choice.title)
                    + (" ✓" if choice.id == self.effort else ""),
                    "detail": f"{choice.id} · {selected.chat_title or selected.title}",
                }
                for choice in selected.efforts
            )
            self.palette_items.append(
                {
                    "id": "effort.default",
                    "title": self.tr("Efor · sağlayıcı varsayılanı"),
                    "detail": self.tr("Ek efor tercihi gönderme"),
                }
            )
        try:
            if mode == "actions":
                self.palette_items.extend(
                    dict(item, title=self.tr(item["title"])) for item in self.list_actions()
                )
        except Exception:
            pass
        unique = {}
        for item in self.palette_items:
            unique.setdefault(item["id"], item)
        self.palette_items = list(unique.values())
        self.palette_visible = True
        self.palette_search.text = ""
        self.filter_palette()
        self.app.layout.focus(self.palette_search)

    def render_palette(self) -> FormattedText:
        rows = []
        width = max(8, self.app.output.get_size().columns - 20)
        title_width = min(38, max(8, width // 2))
        for index, item in enumerate(self.palette_filtered):
            selected = index == self.palette_index
            style = "class:palette.selected" if selected else "class:palette.item"
            title = str(item.get("title") or item.get("id"))
            detail = str(item.get("detail") or item.get("id"))
            title = fit_text(title, title_width)
            title += " " * max(0, title_width - get_cwidth(title))
            rows.append(
                (style, fit_text(f" {'›' if selected else ' '} {title} {detail}", width) + "\n")
            )
        if not rows:
            message = self.tr("Eşleşen komut yok")
            if self.palette_mode == "models":
                message = (
                    self.tr("Model kataloğu yükleniyor…")
                    if self.catalog_loading
                    else self.tr("Model kataloğu boş veya alınamadı: {0}").format(
                        self.catalog_error or "F10 ile yenileyin"
                    )
                )
            elif self.palette_mode == "effort":
                message = self.tr(
                    "Önce /model ile model seçin; efor seçenekleri seçili modele bağlıdır"
                )
            rows.append(("class:sidebar.muted", f"  {message}\n"))
        return FormattedText(
            [
                (*fragment, on_click(lambda i=i: self.click_palette(i)))
                for i, fragment in enumerate(rows)
            ]
        )

    def filter_palette(self) -> None:
        query = (
            self.palette_search.text.strip().casefold() if hasattr(self, "palette_search") else ""
        )
        self.palette_filtered = [
            item
            for item in self.palette_items
            if query
            in f"{item.get('id', '')} {item.get('title', '')} {item.get('detail', '')}".casefold()
        ]
        self.palette_index = min(self.palette_index, max(0, len(self.palette_filtered) - 1))
        if hasattr(self, "app"):
            self.app.invalidate()

    def execute_palette_item(self) -> None:
        if not self.palette_filtered:
            return
        item = self.palette_filtered[self.palette_index]
        self.palette_visible = False
        self.app.layout.focus(self.input)
        action_id = item["id"]
        if action_id in self.action_handlers:
            self.dispatch_action(action_id)
            return
        if action_id in ("layout.split", "layout.split_down", "layout.unsplit"):
            self.handle_command(
                {
                    "layout.split": "/split",
                    "layout.split_down": "/split-down",
                    "layout.unsplit": "/unsplit",
                }[action_id]
            )
            return
        if action_id.startswith("project.delete:"):
            self.delete_record("container", action_id.partition(":")[2])
            return
        if action_id.startswith("project.edit:"):
            self.project_panel.edit(action_id.partition(":")[2])
            return
        if action_id.startswith("project.select:"):
            self.choose_project(action_id.partition(":")[2])
            return
        if action_id == "project.more":
            self.refresh_projects(more=True)
            self.open_project_picker(getattr(self, "project_picker_purpose", "select"))
            return
        if action_id == "command.temporary":
            self.new_temporary_chat()
            return
        if action_id.startswith("model.account:"):
            key, model_id = action_id[len("model.account:") :].split(":", 1)
            if self.conversation_id or self.messages:
                self.status = self.tr(
                    "Mevcut sohbetin sağlayıcısı değiştirilemez; yeni sohbet açın"
                )
                return
            self.account_service.select(key)
            self.bind_account(key)
            self.restore_account_preference()
            if Capability.TEMPORARY not in self.provider_features:
                self.temporary = False
            self.current_project = {}
            self.select_model(model_id)
            return
        if action_id.startswith("model.select:"):
            selected_model = action_id.partition(":")[2]
            self.select_model(selected_model)
            return
        if action_id.startswith("effort.select:") or action_id == "effort.default":
            self.select_effort(action_id.partition(":")[2] if ":" in action_id else None)
            return
        if action_id.startswith("command."):
            self.handle_command(
                {
                    "command.new": "/new",
                    "command.resume": "/resume",
                    "command.search": "/search",
                    "command.model": "/model",
                    "command.effort": "/effort",
                    "command.projects": "/projects",
                    "command.project": "/project",
                    "command.refresh": "/refresh",
                    "command.next_tab": "/tab-next",
                    "command.previous_tab": "/tab-prev",
                    "command.close_tab": "/tab-close",
                    "command.actions": "/actions",
                    "command.help": "/help",
                    "command.reload": "/history",
                    "command.rename": "/rename",
                    "command.copy": "/copy",
                    "command.remove": "/remove",
                    "command.action": "/action",
                    "command.clear": "/clear",
                    "command.settings": "/settings",
                    "command.downloads": "/downloads",
                    "command.session": "/session",
                    "command.quit": "/exit",
                    "command.quit_alias": "/quit",
                }[action_id]
            )
            return
        if action_id == "ui.focus_sidebar":
            self.focus_sidebar()
            return
        if action_id == "ui.toggle_sidebar":
            self.toggle_sidebar()
            return
        if self.invoke_action is None:
            self.status = self.tr("Action {0} cannot be invoked from this view").format(action_id)
            self.app.invalidate()
            return
        self.run_action(action_id)

    def close_palette(self) -> None:
        self.close_overlays()

    def close_overlays(self) -> None:
        self.palette_visible = self.resume_visible = False
        self.settings_dialog.visible = self.tab_kind == "settings"
        self.session_dialog.visible = (
            self.tab_kind == "settings" and self.settings_dialog.category == "accounts"
        )
        self.app.layout.focus(
            self.settings_dialog.category_control
            if self.tab_kind == "settings"
            else self.downloads_panel.search
            if self.tab_kind == "downloads"
            else self.input
        )
        self.app.invalidate()

    def request_quit(self):
        dirty = bool(self.input.text) or any(tab.get("draft") for tab in self.tabs)
        if (dirty or self.busy) and time.monotonic() > getattr(self, "quit_armed_until", 0):
            self.quit_armed_until = time.monotonic() + 3
            self.status = self.tr(
                "Çıkmak için 3 saniye içinde Ctrl+D veya Ctrl+Q'ya tekrar basın · taslak/yanıt açık"
            )
            self.app.invalidate()
            return
        self.app.exit()

    def active_busy(self) -> bool:
        tabs = getattr(self, "tabs", [])
        index = getattr(self, "active_tab", 0)
        return bool(
            (tabs and tabs[index].get("busy")) or (self.busy and self.busy_tab_index == index)
        )

    @staticmethod
    def _cancel_connection(connection: Any) -> None:
        if connection is None:
            return
        try:
            connection.shutdown(2)
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass

    def stop_active_response(self) -> bool:
        if not self.active_busy():
            return False
        tab = self.tabs[self.active_tab]
        tab["cancel_requested"] = True
        self.stop_confirmation_until = 0.0
        self.stop_confirmation_tab_id = None
        self.status = self.tr("Yanıt durduruluyor…")
        self._cancel_connection(tab.get("connection"))
        self.app.invalidate()
        return True

    def handle_stop_escape(self) -> bool:
        if not self.active_busy():
            self.stop_confirmation_until = 0.0
            self.stop_confirmation_tab_id = None
            return False
        now = time.monotonic()
        tab_id = self.tabs[self.active_tab].get("tab_id")
        if tab_id == self.stop_confirmation_tab_id and now <= self.stop_confirmation_until:
            self.stop_active_response()
            return True
        self.stop_confirmation_until = now + 3.0
        self.stop_confirmation_tab_id = tab_id
        self.status = self.tr("Yanıtı durdurmak için Esc'ye tekrar basın · 3 sn")
        self.app.invalidate()
        return True

    def _sync_activity(self) -> None:
        running = [i for i, tab in enumerate(self.tabs) if tab.get("busy")]
        self.busy = bool(running) or any(tab.get("busy") for tab in self.background_tabs)
        self.busy_tab_index = (
            self.active_tab if self.active_tab in running else running[0] if running else None
        )
        tab = self.tabs[self.active_tab]
        self.active_started = tab.get("started") if tab.get("busy") else None
        self.cancel_connection = tab.get("connection") if tab.get("busy") else None

    def _save_active_tab(self) -> None:
        previous = self.tabs[self.active_tab]
        state = self._new_tab_state()
        state["tab_id"] = previous.get("tab_id") or uuid.uuid4().hex
        previous.update(state)

    def new_tab(self) -> None:
        self.close_overlays()
        if self.loading_conversation and not self.account_service:
            self.status = self.tr("Wait for the current load before creating a tab")
            self.app.invalidate()
            return
        self._save_active_tab()
        self.tabs.append(
            {
                "account_key": self.account_key,
                "catalog": self.catalog,
                "effort_preference": self.effort_preference,
                "conversation_id": None,
                "title": self.tr("New conversation"),
                "messages": [],
                "model": self.model,
                "effort": self.effort,
                "project": {},
                "busy": False,
                "completed_pending": False,
            }
        )
        self.active_tab = len(self.tabs) - 1
        self._restore_active_tab()
        if self.account_service and hasattr(self.account_service, "active_accounts"):
            selected = self.account_service.active_accounts()
            if self.account_key not in {item["key"] for item in selected}:
                key = next(
                    (item["key"] for item in selected if item["provider"] == self.provider_id),
                    self.account_service.active(),
                )
                self.bind_account(key)
                self.model = (
                    self.catalog.default_chat_model.id if self.catalog.default_chat_model else ""
                )
                self.effort = None
                if self.catalog.state == FeatureState.UNKNOWN:
                    self.refresh_provider_catalog()
        self.restore_account_preference()
        self._save_active_tab()

    def switch_tab(self, offset: int) -> None:
        self.close_overlays()
        if (self.loading_conversation and not self.account_service) or len(self.tabs) < 2:
            if self.loading_conversation:
                self.status = self.tr("Wait for the current load before switching tabs")
                self.app.invalidate()
            return
        self._save_active_tab()
        self.active_tab = (self.active_tab + offset) % len(self.tabs)
        self._restore_active_tab()

    def _restore_active_tab(self, *, focus: bool = True) -> None:
        self.transcript_control.clear_selection()
        tab = self.tabs[self.active_tab]
        tab.setdefault("tab_id", uuid.uuid4().hex)
        if self.account_service:
            self.bind_account(tab.get("account_key", self.account_key))
            self.catalog = (
                self._account_catalogs.get(self.account_key)
                or tab.get("catalog")
                or ModelCatalog(FeatureState.UNKNOWN)
            )
            self.catalog_loading = self.account_key in self._catalog_requests
        self.temporary = bool(tab.get("temporary"))
        self.loading_conversation = bool(tab.get("loading"))
        self.loading_started = tab.get("loading_started")
        self.message_selection = False
        self.selected_message_ref = None
        self.tab_kind = tab.get("kind", "chat")
        self.settings_dialog.visible = self.tab_kind == "settings"
        self.session_dialog.visible = (
            self.tab_kind == "settings" and self.settings_dialog.category == "accounts"
        )
        self.conversation_id = tab["conversation_id"]
        self.title = tab["title"]
        self.messages = tab["messages"]
        self.model = tab["model"]
        self.effort = tab.get("effort")
        self.last_response_model = tab.get("response_model")
        self.effort_preference = tab.get("effort_preference", self.effort_preference)
        self.normalize_model_selection()
        tab.update(model=self.model, effort=self.effort, catalog=self.catalog)
        self.input.text = tab.get("draft", "")
        self.current_project = dict(tab.get("project") or {})
        self._sync_activity()
        completed_pending = bool(tab.get("completed_pending"))
        tab["completed_pending"] = False
        if self.set_active_conversation is not None:
            self.set_active_conversation(None if self.temporary else self.conversation_id)
        if tab.get("loading"):
            self.status = self.tr("Loading conversation…")
        elif tab.get("busy"):
            self.status = self.tr("Streaming response…")
        elif self.busy and self.busy_tab_index is not None:
            self.status = self.tr("Yanıt {0}. sekmede sürüyor").format(self.busy_tab_index + 1)
        elif completed_pending:
            self.status = self.tr("Yanıt bu sekmede tamamlandı")
        else:
            self.status = self.tr("Sekme {0}/{1}").format(self.active_tab + 1, len(self.tabs))
        self.follow_output = tab.get("follow_output", True)
        self.transcript_window.vertical_scroll = (
            10**9 if self.follow_output else tab.get("scroll", 0)
        )
        self.app.invalidate()
        if focus:
            self.app.layout.focus(
                self.settings_dialog.category_control
                if self.tab_kind == "settings"
                else self.downloads_panel.search
                if self.tab_kind == "downloads"
                else self.input
            )
        if self.account_service and self.catalog.state == FeatureState.UNKNOWN:
            self.refresh_provider_catalog()

    def close_tab(self) -> None:
        self.close_overlays()
        if (
            (self.loading_conversation and not self.account_service)
            or self.uploading_image
            or self.background_jobs
        ):
            self.status = self.tr("Wait for active work to finish before closing a tab")
            self.app.invalidate()
            return
        closing_id = self.tabs[self.active_tab].get("tab_id")
        protected_attachments = self.pending_attachments + self._inflight_attachments
        if self._attachments_in_flight and any(
            item.get("source_tab_id") == closing_id for item in protected_attachments
        ):
            self.status = self.tr("Ekler gönderilirken kaynak sekme kapatılamaz")
            self.app.invalidate()
            return
        self._save_active_tab()
        closing = self.tabs[self.active_tab]
        closing_id = closing.get("tab_id")
        discarded = [
            item for item in self.pending_attachments if item.get("source_tab_id") == closing_id
        ]
        if discarded:
            self.pending_attachments = [
                item for item in self.pending_attachments if item.get("source_tab_id") != closing_id
            ]
            self._release_staged_attachments(discarded)
        self._discard_tab_render_cache(closing)
        if closing.get("busy"):
            self.background_tabs.append(closing)
        if (
            not closing.get("busy")
            and closing.get("temporary")
            and closing.get("conversation_id")
            and self.forget_temporary
        ):
            remote_id = closing["conversation_id"]
            self._read_in_background(
                lambda: self.forget_temporary(remote_id),
                lambda _: None,
                lambda _: None,
                session_bound=False,
            )
        if len(self.tabs) == 1:
            self.tabs[0] = {
                "conversation_id": None,
                "title": self.tr("New conversation"),
                "messages": [],
                "model": self.model,
                "effort": self.effort,
                "project": {},
                "busy": False,
                "completed_pending": False,
            }
            self.active_tab = 0
        else:
            self.tabs.pop(self.active_tab)
            self.active_tab = min(self.active_tab, len(self.tabs) - 1)
        self._restore_active_tab()

    def close_all_tabs(self) -> None:
        """Close idle tabs atomically; retain a clean draft as the last tab."""
        self._save_active_tab()
        if (
            self.busy
            or self.uploading_image
            or self.background_jobs
            or any(tab.get("busy") or tab.get("loading") for tab in self.tabs)
        ):
            self.status = self.tr(
                "Sekmeleri kapatmadan önce devam eden işlemin tamamlanmasını bekleyin"
            )
            self.app.invalidate()
            return
        cleanup = []
        self._release_staged_attachments(self.pending_attachments)
        self.pending_attachments.clear()
        for tab in self.tabs:
            self._discard_tab_render_cache(tab)
        for tab in self.tabs:
            if tab.get("temporary") and tab.get("conversation_id"):
                callback = (
                    self.account_service.callbacks(tab.get("account_key", self.account_key)).get(
                        "forget_temporary"
                    )
                    if self.account_service
                    else self.forget_temporary
                )
                if callback:
                    cleanup.append((callback, tab["conversation_id"]))
        self.close_overlays()
        self.tabs = [
            {
                "account_key": self.account_key,
                "conversation_id": None,
                "title": self.tr("Yeni sohbet"),
                "messages": [],
                "model": self.model,
                "effort": self.effort,
                "project": {},
                "busy": False,
                "completed_pending": False,
            }
        ]
        self.active_tab = 0
        self._restore_active_tab()
        for callback, remote_id in cleanup:
            self._read_in_background(
                lambda callback=callback, remote_id=remote_id: callback(remote_id),
                lambda _: None,
                lambda reason: setattr(
                    self, "status", self.tr("Geçici sohbet temizlenemedi: ") + reason
                ),
                session_bound=False,
            )
        if self.layout_controller:
            self.layout_controller.pending = None
            self.layout_controller.active_group()
        self.status = self.tr("Sekmeler kapatıldı")
        self.app.invalidate()

    def _discard_tab_render_cache(self, tab):
        """Release closed-tab data; in-flight jobs retain only their own working state."""
        retry_handle = getattr(self, "_virtual_retry_handle", None)
        if retry_handle is not None:
            retry_handle.cancel()
            self._virtual_retry_handle = None
        tab_id = tab.get("tab_id")
        release_pane = getattr(self.layout_controller, "release", None)
        if release_pane:
            release_pane(tab_id)
        tab.pop("history_read", None)
        tab.pop("history_anchor", None)
        self.transcript_control.clear_selection()
        archive = self.archive_service()
        if archive and tab.get("conversation_id"):
            key, remote = origin(tab["conversation_id"], tab.get("account_key", self.account_key))
            archive.release(key, remote)
        if not tab.get("busy"):
            tab["messages"] = []
        getattr(self, "_prepared_transcripts", {}).pop(tab_id, None)
        self.renderer.clear_cache(wait=False)
        if getattr(self, "_virtual_tab_id", None) == tab_id:
            layout = getattr(self, "_virtual_transcript", None)
            if layout is not None:
                layout.dispose()
            self._virtual_transcript = None
            self._virtual_tab_id = None
            self._virtual_conversation_id = None
            self._virtual_signature = None
            self._virtual_pending_signature = None
            self._virtual_prefetch_key = None
            self._virtual_prefetch_done_key = None
            self.transcript_control.rows = ()
        self._transcript_cache = ""
        self._transcript_signature = None
        self._transcript_lines = ()
        self._line_render_key = None
        self._ansi_fragments = ()
        self._ansi_render_key = None

    def viewport_height(self) -> D:
        try:
            rows = self.app.output.get_size().rows
        except Exception:
            rows = shutil.get_terminal_size((100, 24)).lines
        return D.exact(max(6, rows))

    @staticmethod
    def format_elapsed(seconds: float) -> str:
        total = max(0, int(seconds))
        if total < 60:
            return f"{total}s"
        return f"{total // 60}m {total % 60:02d}s"

    def render_activity(self) -> FormattedText:
        stop_hint = self.tr("Yanıtı durdurmak için Esc'ye tekrar basın · 3 sn")
        if self.active_busy() and self.stop_confirmation_tab_id == self.tabs[self.active_tab].get(
            "tab_id"
        ):
            if time.monotonic() <= self.stop_confirmation_until:
                return FormattedText([("", f" {stop_hint} ")])
            self.stop_confirmation_until = 0.0
            self.stop_confirmation_tab_id = None
        if self.busy and self.busy_tab_index is None:
            return FormattedText([("class:sidebar.running", self.tr("Yanıt arka planda sürüyor"))])
        if self.busy and self.busy_tab_index != self.active_tab:
            running = (self.busy_tab_index or 0) + 1
            return FormattedText(
                [
                    (
                        "",
                        self.tr(
                            " ● Yanıt {0}. sekmede sürüyor · diğer sekmelere geçebilirsiniz "
                        ).format(running),
                    )
                ]
            )
        started = (
            self.active_started
            if self.busy and self.busy_tab_index == self.active_tab
            else self.loading_started
            if self.loading_conversation
            else self.upload_started
        )
        if started is not None:
            frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            frame = frames[int((time.monotonic() - started) * 10) % len(frames)]
            elapsed = self.format_elapsed(time.monotonic() - started)
            phase = (
                self.tr("Yanıt üretiliyor")
                if self.busy
                else self.tr("Sohbet yükleniyor")
                if self.loading_conversation
                else self.tr("Dosya yükleniyor")
            )
            detail = "" if self.status == stop_hint else " · " + self.status
            return FormattedText([("", f" {frame} {phase} · {elapsed}{detail} ")])
        archive = self.archive_service()
        if archive:
            states = self.sync_states(archive)
            pending_sync = [x for x in states if x["state"] == "syncing"]
            failures = [x for x in states if x["state"] == "error"]
            if pending_sync:
                return FormattedText(
                    [
                        (
                            "class:sync",
                            self.tr(" ↻ Senkronizasyon · {0} kayıt · {1} ").format(
                                len(pending_sync), self.status
                            ),
                        )
                    ]
                )
            if failures:
                return FormattedText(
                    [
                        (
                            "class:sync.error",
                            self.tr(
                                " ! {0} kayıt senkronize edilemedi · önbellek korunuyor "
                            ).format(len(failures)),
                        )
                    ]
                )
        pending = [
            label
            for label, active in (
                (self.tr("Modeller"), self.catalog_loading),
                (self.tr("Sohbetler"), self.sidebar_loading),
                (self.tr("Projeler"), self.projects_loading),
            )
            if active
        ]
        if pending:
            status = self.status if self.status not in ("Ready", "") else self.tr("Hazır")
            return FormattedText(
                [("", self.tr(" • {0} · {1} yenileniyor").format(status, ", ".join(pending)))]
            )
        if self.status not in ("Ready", ""):
            return FormattedText([("", f" • {self.status} ")])
        if self.pending_attachments:
            return FormattedText(
                [
                    (
                        "",
                        " 📎 "
                        + self.tr("{0} dosya hazır").format(len(self.pending_attachments))
                        + " ",
                    )
                ]
            )
        if self.last_elapsed is not None:
            return FormattedText(
                [
                    (
                        "",
                        self.tr(" — Worked for {0} ").format(
                            self.format_elapsed(self.last_elapsed)
                        ),
                    ),
                ]
            )
        return FormattedText([("", self.tr(" Hazır"))])

    def remember_transcript(
        self,
        tab,
        messages,
        width,
        tokens,
        reasoning,
        text,
        ranges,
        ansi,
        lines=None,
        *,
        message_key=None,
        revision=0,
    ):
        cache = getattr(self, "_prepared_transcripts", {})
        self._prepared_transcripts = cache
        key = tab["tab_id"]
        cache.pop(key, None)
        roles = tuple(message["role"] for message in messages)
        cost = (
            len(text.encode("utf-8"))
            + sum(len(part[1].encode("utf-8")) for part in ansi)
            + len(roles) * 8
        )
        budget = 1_000_000
        if cost > budget:
            return
        cache[key] = dict(
            width=width,
            conversation_id=tab.get("conversation_id"),
            tokens=tokens,
            reasoning=reasoning,
            message_key=message_key or self._virtual_message_key(tab.get("messages", [])),
            revision=revision,
            roles=roles,
            text=text,
            ranges=ranges,
            ansi=ansi,
            lines=lines if lines is not None else index_transcript(ansi, ranges),
            cost=cost,
        )
        while cache and sum(item["cost"] for item in cache.values()) > budget:
            cache.pop(next(iter(cache)))

    def render_transcript(self) -> str:
        width = self.transcript_width()
        with self.lock:
            tab = self.tabs[self.active_tab]
            message_key = self._virtual_message_key(self.messages)
            revision = tab.get("render_revision", 0)
            load_state = (
                bool(tab.get("loading")),
                bool(tab.get("syncing")),
                tab.get("load_error"),
            )
            signature = (
                width,
                self.busy,
                self.busy_tab_index,
                self.active_tab,
                self.show_reasoning,
                tab.get("tab_id"),
                tab.get("conversation_id"),
                message_key,
                revision,
                load_state,
            )
            if signature == getattr(self, "_transcript_signature", None):
                return self._transcript_cache
            messages = [dict(message) for message in self.messages]
        self._transcript_roles = tuple(message.get("role") for message in messages)
        snapshots = getattr(self, "_prepared_transcripts", {})
        tab_id = self.tabs[self.active_tab].get("tab_id")
        snapshot = snapshots.get(tab_id)
        if (
            snapshot
            and snapshot["width"] == width
            and snapshot["tokens"] == self.renderer.tokens
            and snapshot["reasoning"] == self.show_reasoning
            and snapshot.get("conversation_id") == tab.get("conversation_id")
            and snapshot["message_key"] == message_key
            and snapshot["revision"] == revision
        ):
            snapshots.pop(tab_id)
            snapshots[tab_id] = snapshot
            self._transcript_roles = snapshot["roles"]
            self.message_ranges = snapshot["ranges"]
            self.transcript_line_count = snapshot["text"].count("\n") + 1
            self._ansi_render_key, self._ansi_fragments = snapshot["text"], snapshot["ansi"]
            self._line_render_key, self._transcript_lines = snapshot["text"], snapshot["lines"]
            self._transcript_signature, self._transcript_cache = signature, snapshot["text"]
            return snapshot["text"]
        if self.app.is_running and messages:
            tab = self.tabs[self.active_tab]
            key = tab["tab_id"]
            pending = getattr(self, "_render_pending", set())
            self._render_pending = pending
            if key not in pending:
                pending.add(key)
                tokens, reasoning = dict(self.renderer.tokens), self.show_reasoning
                conversation_id = tab.get("conversation_id")
                message_key = self._virtual_message_key(tab.get("messages", []))
                revision = tab.get("render_revision", 0)

                def read():
                    return prepare_transcript(
                        messages,
                        width,
                        tokens,
                        reasoning,
                        cancelled=lambda: (
                            not any(tab is x for x in self.tabs)
                            or self.tabs[self.active_tab] is not tab
                            or tab.get("conversation_id") != conversation_id
                        ),
                    )

                def ready(result):
                    pending.discard(key)
                    if (
                        tab.get("conversation_id") == conversation_id
                        and self._virtual_message_key(tab.get("messages", [])) == message_key
                        and tab.get("render_revision", 0) == revision
                    ):
                        self.remember_transcript(
                            tab,
                            messages,
                            width,
                            tokens,
                            reasoning,
                            *result,
                            message_key=message_key,
                            revision=revision,
                        )
                    self.app.invalidate()

                def failed(reason):
                    pending.discard(key)
                    self.app.invalidate()

                self._read_in_background(read, ready, failed, session_bound=False, cpu=True)
            if (
                snapshot
                and snapshot.get("conversation_id") == tab.get("conversation_id")
                and snapshot["width"] == width
                and snapshot["tokens"] == self.renderer.tokens
                and snapshot["reasoning"] == self.show_reasoning
            ):
                self._transcript_roles = snapshot["roles"]
                self.message_ranges = snapshot["ranges"]
                self.transcript_line_count = len(snapshot["lines"])
                self._line_render_key, self._transcript_lines = snapshot["text"], snapshot["lines"]
                return snapshot["text"]
            self.message_ranges = []
            self.transcript_line_count = 1
            return self.tr("Sohbet görünümü hazırlanıyor…")
        if not messages:
            self.message_ranges = []
            buffer = StringIO()
            console = Console(
                file=buffer,
                force_terminal=True,
                color_system="truecolor",
                no_color=False,
                width=width,
                theme=self.renderer.theme,
            )
            console.print(
                Text(
                    (
                        self.tr("Conversation load failed: {0}").format(
                            self.tabs[self.active_tab]["load_error"]
                        )
                        if self.tabs and self.tabs[self.active_tab].get("load_error")
                        else self.tr("Loading conversation…")
                        if self.loading_conversation
                        or (
                            self.tabs
                            and (
                                self.tabs[self.active_tab].get("loading")
                                or self.tabs[self.active_tab].get("syncing")
                            )
                        )
                        else self.tr("This saved conversation has no messages.")
                        if self.tabs and self.tabs[self.active_tab].get("conversation_id")
                        else self.tr("SiopeChat\n\nYeni bir sohbet başlatın.")
                    ),
                    style="dim",
                )
            )
            rendered = buffer.getvalue()
        else:
            active_index = (
                len(messages) - 1 if self.busy and self.busy_tab_index == self.active_tab else None
            )
            rendered, self.message_ranges = self.renderer.transcript(
                messages, width, self.show_reasoning, active_index=active_index
            )
        self.transcript_line_count = rendered.count("\n") + 1
        self._transcript_signature = signature
        self._transcript_cache = rendered
        return rendered

    def terminal_width(self) -> int:
        try:
            return self.app.output.get_size().columns
        except Exception:
            return shutil.get_terminal_size((100, 24)).columns

    def sidebar_width(self) -> int:
        preferred = getattr(self, "theme_options", {}).get("sidebar_width")
        return (
            max(16, min(preferred, self.terminal_width() - 24))
            if preferred
            else max(22, min(30, self.terminal_width() // 4))
        )

    def workspace_width(self) -> int:
        return max(
            1, self.terminal_width() - (self.sidebar_width() + 1 if self.sidebar_visible else 0)
        )

    def main_width(self) -> int:
        columns = (
            self.layout_controller.columns()
            if self.layout_controller and self.layout_enabled
            else 1
        )
        return max(8, (self.workspace_width() - columns + 1) // columns)

    def begin_split(self, axis="columns"):
        if self.layout_controller:
            self.layout_controller.begin(axis)
        else:
            self.status = self.tr("Layout eklentisi kullanılamıyor")
            self.app.invalidate()

    def transcript_width(self) -> int:
        return min(
            getattr(self, "theme_options", {}).get("message_width", 100),
            max(8, self.main_width() - 5),
        )

    def transcript_rows(self, viewport_height=None):
        if self.app.is_running and self.messages:
            return self._virtual_transcript_rows(viewport_height or 1)
        rendered = self.render_transcript()
        reference = getattr(self, "selected_message_ref", None)
        if reference is not None:
            self.selected_message = next(
                (i for i, item in enumerate(self.messages) if item is reference),
                self.selected_message,
            )
        if rendered != getattr(self, "_line_render_key", None):
            if rendered != getattr(self, "_ansi_render_key", None):
                self._ansi_render_key, self._ansi_fragments = (
                    rendered,
                    transcript_fragments(rendered),
                )
            self._transcript_lines = index_transcript(self._ansi_fragments, self.message_ranges)
            self._line_render_key = rendered
        return self._transcript_lines

    @staticmethod
    def _virtual_message_key(messages):
        """Track the transcript's supported mutation paths without scanning it."""
        if not messages:
            return id(messages), 0, None, None, None
        first = messages[0]
        last = messages[-1]
        return id(messages), len(messages), id(first), id(last), id(last.get("text"))

    def _virtual_transcript_rows(self, viewport_height):
        """Prepare transcript layout off-thread and retain only viewport pages."""
        from .transcript import VirtualTranscriptRows

        tab = self.tabs[self.active_tab]
        tab_id = tab.get("tab_id")
        width = self.transcript_width()
        reasoning = self.show_reasoning
        with self.lock:
            messages = self.messages
            message_key = self._virtual_message_key(messages)
            active_index = (
                len(messages) - 1
                if self.busy and self.busy_tab_index == self.active_tab and messages
                else None
            )
        conversation_id = tab.get("conversation_id")
        signature = (
            tab_id,
            conversation_id,
            width,
            reasoning,
            repr(self.renderer.tokens),
            message_key,
            active_index,
            tab.get("render_revision", 0),
        )
        layout = getattr(self, "_virtual_transcript", None)
        layout_is_current = (
            layout is not None and getattr(self, "_virtual_signature", None) == signature
        )
        if not layout_is_current:
            if (
                getattr(self, "_virtual_tab_id", None) != tab_id
                or getattr(self, "_virtual_conversation_id", None) != conversation_id
            ):
                if layout is not None:
                    layout.dispose()
                self._virtual_transcript = None
                self._virtual_pending_signature = None
                layout = None
            self._virtual_tab_id = tab_id
            self._virtual_conversation_id = conversation_id
            self._request_virtual_transcript(
                tab, width, reasoning, signature, active_index, viewport_height
            )
            layout = getattr(self, "_virtual_transcript", None)
        if layout is None:
            self.message_ranges = []
            self.transcript_line_count = 1
            return (([("class:sidebar.muted", self.tr("Sohbet görünümü hazırlanıyor…"))], None),)

        self.message_ranges = layout.ranges
        self.transcript_line_count = layout.line_count
        structure_key = (tab_id, *message_key[:4])
        if structure_key != getattr(self, "_virtual_selection_key", None):
            with self.lock:
                self._transcript_roles = tuple(message.get("role") for message in self.messages)
                reference = getattr(self, "selected_message_ref", None)
                if reference is not None:
                    self.selected_message = next(
                        (i for i, item in enumerate(self.messages) if item is reference),
                        self.selected_message,
                    )
            self._virtual_selection_key = structure_key
        rows = VirtualTranscriptRows(layout)
        scroll = self.transcript_window.vertical_scroll
        if scroll > layout.line_count:
            scroll = max(0, layout.line_count - viewport_height)
        if layout_is_current:
            self.history_pager.prefetch(layout, scroll, viewport_height)
        overscan = 2 * viewport_height
        start = max(0, scroll - overscan)
        stop = min(layout.line_count, scroll + viewport_height + overscan)
        prefetch_key = (id(layout), start, stop)
        self._virtual_prefetch_key = prefetch_key
        if prefetch_key != getattr(self, "_virtual_prefetch_done_key", None) and not getattr(
            self, "_virtual_prefetch_inflight", False
        ):
            self._virtual_prefetch_inflight = True
            self._virtual_prefetch_running_key = prefetch_key

            def prefetch_finished(_result=None):
                if self._virtual_prefetch_running_key == prefetch_key:
                    self._virtual_prefetch_inflight = False
                    self._virtual_prefetch_done_key = (
                        prefetch_key if self._virtual_prefetch_key == prefetch_key else None
                    )
                    self.app.invalidate()

            def prefetch():
                def cancelled():
                    return (
                        self._virtual_prefetch_key != prefetch_key
                        or getattr(self, "_virtual_transcript", None) is not layout
                    )

                layout.prefetch(scroll, min(layout.line_count, scroll + viewport_height), cancelled)
                if not cancelled():
                    layout.prefetch(start, stop, cancelled)

            self._read_in_background(
                prefetch,
                prefetch_finished,
                lambda _: prefetch_finished(),
                session_bound=False,
                cpu=True,
            )
        return rows

    def _request_virtual_transcript(
        self, tab, width, reasoning, signature, active_index, viewport_height
    ):
        if getattr(
            self, "_virtual_failed_signature", None
        ) == signature and time.monotonic() < getattr(self, "_virtual_retry_after", 0):
            return
        pending = getattr(self, "_virtual_pending_signature", None)
        if pending is not None:
            return
        if self.busy:
            elapsed = time.monotonic() - getattr(self, "_virtual_last_request_at", 0)
            delay = 0.08 - elapsed
            if delay > 0:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    pass
                else:
                    if getattr(self, "_virtual_retry_handle", None) is not None:
                        return

                    def retry():
                        self._virtual_retry_handle = None
                        self.app.invalidate()

                    self._virtual_retry_handle = loop.call_later(delay, retry)
                    return
        self._virtual_last_request_at = time.monotonic()
        self._virtual_pending_signature = signature
        conversation_id = tab.get("conversation_id")
        scroll = self.transcript_window.vertical_scroll
        tab_id = tab.get("tab_id")
        tokens = dict(self.renderer.tokens)
        anchor = tab.get("history_anchor")
        previous = getattr(self, "_virtual_transcript", None)
        shared_renderer = (
            previous.renderer
            if previous is not None
            and previous.width == width
            and previous.renderer.tokens == tokens
            else None
        )

        with self.lock:
            if self._virtual_message_key(self.messages) != signature[5]:
                self._virtual_pending_signature = None
                return
            messages = tuple(
                (message.get("role"), message.get("text", "")) for message in self.messages
            )
            ordinals = tuple(message.get("_archive_ordinal") for message in self.messages)

        def still_active():
            current_active_index = (
                len(self.messages) - 1
                if self.busy and self.busy_tab_index == self.active_tab and self.messages
                else None
            )
            return (
                any(item is tab for item in self.tabs)
                and self.tabs[self.active_tab] is tab
                and self.transcript_width() == width
                and self.show_reasoning == reasoning
                and repr(self.renderer.tokens) == signature[4]
                and self._virtual_message_key(self.messages) == signature[5]
                and current_active_index == active_index
                and tab.get("render_revision", 0) == signature[7]
            )

        def read():
            value = prepare_virtual_transcript(
                messages,
                width,
                tokens,
                reasoning,
                cancelled=lambda: not still_active(),
                active_index=active_index,
                renderer=shared_renderer,
            )
            if value.line_count:
                top = self.history_pager.position(value, ordinals, anchor, viewport_height, scroll)
                value.prefetch(
                    top,
                    min(value.line_count, top + viewport_height),
                    cancelled=lambda: not still_active(),
                )
            return value

        def ready(value):
            if self._virtual_pending_signature == signature:
                self._virtual_pending_signature = None
            if still_active() and tab.get("tab_id") == tab_id:
                self._virtual_failed_signature = None
                previous = getattr(self, "_virtual_transcript", None)
                if previous is not None and previous is not value:
                    previous.dispose(retain_renderer=previous.renderer is value.renderer)
                self._virtual_transcript = value
                if anchor is not None and tab.get("history_anchor") is anchor:
                    self.transcript_window.vertical_scroll = self.history_pager.position(
                        value, ordinals, anchor, viewport_height, scroll
                    )
                    tab.pop("history_anchor", None)
                self._virtual_signature = signature
                self._virtual_tab_id = tab_id
                self._virtual_conversation_id = conversation_id
                self._virtual_prefetch_key = None
                self._virtual_prefetch_done_key = None
                self.app.invalidate()
            else:
                current = getattr(self, "_virtual_transcript", None)
                value.dispose(
                    retain_renderer=current is not None and current.renderer is value.renderer
                )

        def failed(_reason):
            if self._virtual_pending_signature == signature:
                self._virtual_pending_signature = None
            self._virtual_failed_signature = signature
            self._virtual_retry_after = time.monotonic() + 1
            self.app.invalidate()

        self._read_in_background(read, ready, failed, session_bound=False, cpu=True)

    def open_message_link(self, url, index=None):
        from .markdown import open_link

        if self.account_service is None:
            open_link(url)
            return
        key, conversation, title = self.account_key, self.conversation_id, self.title
        message = (
            self.messages[index] if index is not None and 0 <= index < len(self.messages) else {}
        )
        message_id = message.get("id")
        self.status = self.tr("Bağlantı hazırlanıyor…")

        def progress(name, size, total):
            label = f"{size // 1024} KiB" + (f" / {total // 1024} KiB" if total else "")

            def report():
                self.status = self.tr("İndiriliyor: {0} · {1}").format(name, label)
                self.app.invalidate()

            self._dispatch_ui(report)

        def completed(result):
            if result["kind"] == "page":
                open_link(result["url"])
                self.status = self.tr("Bağlantı tarayıcıda açıldı")
            else:
                self.status = self.tr("İndirildi: {0}").format(result["path"])
            self.app.invalidate()

        def failed(reason):
            import re

            status = re.fullmatch(r"Dosya alınamadı \(HTTP (\d+)\)", str(reason))
            detail = (
                self.tr("Dosya alınamadı (HTTP {0})").format(status[1])
                if status
                else self.tr(str(reason))
            )
            self.status = self.tr("İndirme başarısız: {0}").format(detail)
            self.app.invalidate()

        self._read_in_background(
            lambda: self.account_service.download(
                key, url, conversation, message_id, progress, title=title
            ),
            completed,
            failed,
            session_bound=False,
        )

    def select_message(self, index):
        if index is None or not 0 <= index < len(self.messages):
            return
        self.selected_message = index
        self.selected_message_ref = self.messages[index]
        self.message_selection = False
        self.pause_following()
        self.app.layout.focus(self.transcript_control)
        self.app.invalidate()

    def composer_line_count(self):
        width = max(1, self.main_width() - 4)
        return (
            sum(
                max(1, (get_cwidth(line) + width - 1) // width)
                for line in self.input.text.split("\n")
            )
            if hasattr(self, "input")
            else 1
        )

    def composer_height(self):
        return min(
            getattr(self, "theme_options", {}).get("composer_max_lines", 6),
            self.composer_line_count(),
        )

    def transcript_cursor_position(self) -> Point:
        if self.follow_output:
            return Point(x=0, y=max(0, self.transcript_line_count - 1))
        return Point(x=0, y=max(0, self.transcript_window.vertical_scroll))

    def render_resume_list(self) -> FormattedText:
        if self.resume_loading:
            return FormattedText([("class:resume.time", self.tr(" Loading conversations…"))])
        if not self.resume_filtered:
            return FormattedText([("class:resume.time", self.tr(" No matching conversations"))])
        fragments: list[tuple[str, str]] = []
        for index, item in enumerate(self.resume_filtered):
            title = item.get("title") or self.tr("Untitled")
            updated = item.get("update_time")
            stamp = ""
            if isinstance(updated, (int, float)):
                try:
                    stamp = datetime.fromtimestamp(updated).strftime("%Y-%m-%d %H:%M")
                except (OSError, OverflowError, ValueError):
                    pass
            marker = "›" if index == self.resume_index else " "
            style = "class:resume.selected" if index == self.resume_index else "class:resume.item"
            width = max(8, self.terminal_width() - 10)
            value = fit_text(f" {marker} {title}  {stamp}", width)
            fragments.append((style, value + "\n", on_click(lambda i=index: self.click_resume(i))))
        return FormattedText(fragments)

    def click_resume(self, index):
        self.resume_index = index
        self.select_resume()

    def pause_following(self) -> None:
        self.follow_output = False

    def transcript_page_size(self) -> int:
        info = self.transcript_window.render_info
        if info is None:
            return 10
        return max(3, info.window_height - 3)

    def append_system(self, text: str) -> None:
        with self.lock:
            self.messages.append({"role": "system", "text": text})
        self.scroll_bottom()

    def scroll_bottom(self, force: bool = True) -> None:
        if force and self.history_pager.request("bottom"):
            return
        if force:
            self.follow_output = True
        if self.follow_output:
            self.transcript_window.vertical_scroll = 10**9
        self.app.invalidate()

    def submit_prompt(self) -> None:
        prompt = self.input.text.strip()
        if prompt.startswith("/"):
            self.input.text = ""
            self.handle_command(prompt)
            return
        if self.account_service and not self.account_key:
            self.status = self.tr("Önce hesap ekleyin")
            self.settings_dialog.open("accounts")
            return
        if (
            self.account_service
            and hasattr(self.account_service, "enabled")
            and not self.account_service.enabled(self.provider_id)
        ):
            self.status = self.tr("Sağlayıcı devre dışı · taslak korundu · Ayarlar → Sağlayıcılar")
            self.app.invalidate()
            return
        self.normalize_model_selection()
        if self.editing_project():
            self.status = self.tr("Önce proje düzenlemesini kaydedin veya iptal edin")
            self.app.invalidate()
            return
        if not self.input.text.lstrip().startswith("/") and (
            self.model.casefold().endswith("-wm")
            or (
                self.catalog.state == FeatureState.AVAILABLE
                and not any(model.id == self.model for model in self.catalog.chat_models)
            )
        ):
            self.status = self.tr("Önce /model ile normal bir sohbet modeli seçin · taslak korundu")
            self.app.invalidate()
            return
        if self.loading_conversation or self.uploading_image:
            self.status = self.tr("Conversation or image still loading · draft kept")
            self.app.invalidate()
            return
        if self.tabs[self.active_tab].get("project_changing"):
            self.status = self.tr("Proje değiştiriliyor · taslak korundu")
            self.app.invalidate()
            return
        if self.temporary and Capability.TEMPORARY not in self.provider_features:
            self.status = self.tr(
                "Bu sağlayıcı geçici sohbeti desteklemiyor · yeni normal sohbet açın"
            )
            self.app.invalidate()
            return
        if (
            self.active_busy()
            or sum(bool(tab.get("busy")) for tab in self.tabs + self.background_tabs) >= 4
        ):
            self.status = self.tr(
                "Bu sohbetin yanıtını bekleyin · aynı anda en fazla 4 bekleyen istek"
            )
            self.app.invalidate()
            return
        if (
            self.account_service
            and hasattr(self.account_service, "active_accounts")
            and not self.input.text.lstrip().startswith("/")
            and self.account_key
            not in {item["key"] for item in self.account_service.active_accounts()}
        ):
            self.status = self.tr(
                "Bu sağlayıcıda başka hesap etkin · Hesaplar’dan bu hesabı seçin · taslak korundu"
            )
            self.app.invalidate()
            return
        if (
            self.current_project.get("metadata", {}).get("read_only")
            and not (
                self.current_project.get("metadata", {}).get("supports_chat")
                or (self.provider_id == "gemini" and self.current_project.get("kind") == "notebook")
            )
            and not self.input.text.lstrip().startswith("/")
        ):
            self.status = self.tr(
                "Bu Gemini defteri salt okunur · defter bağlamında gönderim henüz desteklenmiyor · taslak korundu"
            )
            self.app.invalidate()
            return
        prompt = self.input.text.strip()
        attachments = list(self.pending_attachments)
        if attachments and self._attachments_in_flight:
            self.status = self.tr("Ekler başka bir sohbete gönderiliyor · bekleyin")
            self.app.invalidate()
            return
        if not prompt and not attachments:
            return
        if self.tab_kind == "project":
            source_tab = self.tabs[self.active_tab]
            project, key = dict(self.current_project), self.account_key
            model, effort = self.model, self.effort
            count = len(self.tabs)
            self.new_tab()
            if len(self.tabs) == count:
                return
            if self.account_service:
                self.bind_account(key)
            self.model, self.effort = model, effort
            self.current_project = project
            self.temporary = False
            # The details tab keeps its content; its submitted draft belongs to the new chat.
            source_tab.update(draft="")
        self.input.text = ""
        if prompt.startswith("/"):
            self.handle_command(prompt)
            return
        for item in self.pending_attachments:
            item.setdefault("queue_id", uuid.uuid4().hex)
        attachments = list(self.pending_attachments)
        if attachments:
            submitted_ids = {str(item["queue_id"]) for item in attachments}
            self.pending_attachments = [
                item
                for item in self.pending_attachments
                if str(item.get("queue_id") or "") not in submitted_ids
            ]
            self._attachments_in_flight = True
            self._inflight_attachments = attachments
        prompt = prompt or self.tr("Please review the attached files.")
        display_prompt = prompt
        if attachments:
            labels = " ".join(f"[📎 {item['name']}]" for item in attachments)
            display_prompt = f"{display_prompt}\n\n{labels}"
        tab = self.tabs[self.active_tab]
        window = tab.get("history_window")
        if window and window["end"] < window["total"]:
            self.messages = []
            tab["messages"] = self.messages
            tab.pop("history_anchor", None)
        with self.lock:
            self.messages.append({"role": "user", "text": display_prompt, "local_pending": True})
            self.messages.append({"role": "assistant", "text": "", "local_pending": True})
        self._save_active_tab()
        self.busy = True
        self.busy_tab_index = self.active_tab
        self.tabs[self.active_tab]["busy"] = True
        self.tabs[self.active_tab]["cancel_requested"] = False
        self.stop_confirmation_until = 0.0
        self.stop_confirmation_tab_id = None
        self._invalidate_sidebar_rows()
        if not self.conversation_id:
            self.title = self.tabs[self.active_tab]["title"] = prompt[:60]
        self.tabs[self.active_tab]["completed_pending"] = False
        self.active_started = time.monotonic()
        self.tabs[self.active_tab]["started"] = self.active_started
        self.tabs[self.active_tab]["connection"] = None
        self.last_elapsed = None
        self.status = self.tr("Yanıt bekleniyor…")
        self.scroll_bottom()
        threading.Thread(
            target=self.run_send,
            kwargs={"send_callback": self.send_prompt, "target_tab": self.tabs[self.active_tab]},
            args=(
                prompt,
                attachments,
                self.active_tab,
                self.messages,
                self.conversation_id,
                self.model,
                self.effort,
                self.current_project.get("id"),
                self.temporary,
            ),
            daemon=True,
        ).start()

    def handle_command(self, command: str) -> None:
        if command.strip() == "/downloads":
            self.downloads_panel.open()
            return
        if command.strip() == "/help" and self.tab_kind in ("settings", "downloads"):
            self.new_tab()
        if command.strip() in ("/login", "/logout", "/session"):
            self.session_dialog.open()
            return
        name, _, argument = command.partition(" ")
        if name in ("/exit", "/quit"):
            self.request_quit()
        elif name == "/new":
            self.new_tab()
        elif name in ("/pane-next", "/pane-prev"):
            if self.layout_controller and self.layout_enabled:
                self.layout_controller.focus_pane(1 if name == "/pane-next" else -1)
        elif name == "/tab-next":
            self.switch_tab(1)
        elif name == "/tab-prev":
            self.switch_tab(-1)
        elif name == "/tabs-close":
            self.close_all_tabs()
        elif name == "/tab-close":
            self.close_tab()
        elif name == "/search":
            self.open_sidebar_search()
        elif name == "/project-new":
            self.open_project_create()
        elif name == "/project-edit":
            self.project_panel.edit()
        elif name == "/project-source-add":
            self.project_panel.add_source()
        elif name == "/project-source-remove":
            self.project_panel.remove_source()
        elif name in ("/attach", "/file"):
            self.open_attachment()
        elif name == "/filter":
            self.open_filters()
        elif name == "/account-add":
            self.add_account()
        elif name in ("/split", "/split-down"):
            self.begin_split("rows" if name == "/split-down" else "columns")
        elif name == "/unsplit":
            if self.layout_controller:
                self.layout_controller.unsplit()
        elif name == "/delete":
            self.delete_record("conversation", argument.strip() or None)
        elif name in ("/project-delete", "/delete-project"):
            if argument.strip():
                self.delete_record("container", argument.strip())
            else:
                self.open_project_picker("delete")
        elif name == "/projects":
            self.show_projects()
        elif name == "/actions":
            self.open_palette()
        elif name == "/settings":
            self.settings_dialog.open()
        elif name == "/model":
            model = argument.strip()
            if not model:
                self.open_palette("models")
            else:
                self.select_model(model)
        elif name == "/effort":
            effort = argument.strip().casefold()
            if effort:
                self.select_effort(None if effort == "default" else effort)
            else:
                self.open_palette("effort")
        elif name in ("/project", "/proje"):
            if argument.strip() == "info":
                self.show_project_details()
            elif argument.strip():
                self.choose_project(argument.strip())
            else:
                self.open_project_picker()
        elif name in ("/temporary", "/private"):
            self.new_temporary_chat()
        elif name in ("/refresh", "/sync"):
            self.synchronize(argument.strip() or "all")
        elif name == "/action":
            action_id, _, raw_arguments = argument.strip().partition(" ")
            if action_id:
                try:
                    arguments = json.loads(raw_arguments) if raw_arguments else {}
                    if not isinstance(arguments, dict):
                        raise ValueError("action arguments must be a JSON object")
                    self.run_action(action_id, arguments)
                except (json.JSONDecodeError, ValueError) as error:
                    self.status = self.tr("Usage: /action ID [JSON_OBJECT] · {0}").format(error)
                    self.app.invalidate()
            else:
                self.open_palette()
        elif name == "/clear":
            with self.lock:
                self.messages.clear()
            self.status = self.tr("Transcript cleared")
            self.scroll_bottom()
        elif name == "/resume":
            if argument.strip():
                self.resume_conversation(argument.strip())
            else:
                self.open_resume()
        elif name == "/history":
            if self.temporary:
                self.status = self.tr("Geçici sohbet yalnız açık oturumda tutulur")
                self.app.invalidate()
                return
            if self.conversation_id:
                self.resume_conversation(self.conversation_id)
            else:
                self.status = self.tr("No saved conversation yet")
                self.app.invalidate()
        elif name == "/rename":
            self.rename_current_conversation(argument)
        elif name == "/copy":
            self.copy_latest_response()
        elif name == "/remove":
            if self.pending_attachments and not self._attachments_in_flight:
                removed = self.pending_attachments.pop()
                self._release_staged_attachments([removed])
                self.status = self.tr("Removed {0}").format(removed["name"])
            elif self._attachments_in_flight:
                self.status = self.tr("Dosyalar gönderilirken ek listesi değiştirilemiyor")
            else:
                self.status = self.tr("No pending image to remove")
            self.app.invalidate()
        elif name == "/help":
            existing = next(
                (message for message in self.messages if message.get("local_help")), None
            )
            if existing is not None:
                with self.lock:
                    self.messages.remove(existing)
                state = self.tabs[self.active_tab].pop("help_view", None)
                if state:
                    self.follow_output, self.transcript_window.vertical_scroll = state
                self.app.invalidate()
                return
            self.tabs[self.active_tab]["help_view"] = (
                self.follow_output,
                self.transcript_window.vertical_scroll,
            )
            labels = {
                "ui.command_palette": self.tr("Komut ve eylem paletini aç"),
                "ui.search": self.tr("Yüklenmiş sohbet listesini ara"),
                "navigation.focus_sidebar": self.tr("Sol paneli göster ve odağı al"),
                "ui.toggle_sidebar": self.tr("Sol paneli göster veya gizle"),
                "chat.new": self.tr("Yeni sekme aç"),
                "chat.close_tab": self.tr("Etkin sekmeyi kapat"),
                "chat.next_tab": self.tr("Sonraki sekmeye geç"),
                "chat.previous_tab": self.tr("Önceki sekmeye geç"),
                "chat.resume": self.tr("Son sohbetleri ara ve aç"),
                "chat.send": self.tr("Mesajı gönder"),
                "chat.newline": self.tr("Mesaja yeni satır ekle"),
                "chat.paste_image": self.tr("Metin veya görsel yapıştır"),
                "edit.select_all": self.tr("Metnin veya odaktaki mesajın tümünü seç"),
                "edit.cut": self.tr("Seçili metni kes"),
                "chat.temporary": self.tr("Yeni geçici sohbet"),
                "chat.interrupt": self.tr("Yanıtı durdur veya taslağı temizle"),
                "chat.stop": self.tr("Yanıtı durdur"),
                "chat.clear_transcript": self.tr("Görünen konuşmayı temizle"),
                "chat.reload": self.tr("Sohbeti eşitle"),
                "chat.copy_response": self.tr("Son yanıtı kopyala"),
                "chat.remove_attachment": self.tr("Son eklenen görseli kaldır"),
                "command.help": self.tr("Kısayol ve komut yardımını göster"),
                "command.rename": self.tr("Etkin sohbeti yeniden adlandır"),
                "command.projects": self.tr("Proje bölümünü göster"),
                "command.project": self.tr("Seçili projenin bilgilerini aç"),
                "command.refresh": self.tr("Verileri eşitle"),
                "command.settings": self.tr("Ayarları göster"),
                "app.quit": self.tr("Uygulamadan çık"),
                "app.quit_eof": self.tr("Boş girişte uygulamadan çık"),
                "navigation.sidebar_up": self.tr("Sol panelde yukarı gezin"),
                "navigation.sidebar_down": self.tr("Sol panelde aşağı gezin"),
                "navigation.sidebar_toggle_group": self.tr(
                    "Seçili proje/bölümü daralt veya genişlet"
                ),
                "navigation.close_overlay": self.tr("Açık pencereyi kapat"),
                "navigation.composer_next": self.tr("Komut tamamlamasını seç"),
                "navigation.focus_composer": self.tr("Mesaj girişine odaklan"),
                "navigation.transcript_up": self.tr("Konuşmada yukarı kaydır"),
                "navigation.transcript_down": self.tr("Konuşmada aşağı kaydır"),
                "navigation.transcript_start": self.tr("Konuşmanın başına git"),
                "navigation.transcript_end": self.tr("Konuşmanın sonuna git"),
            }
            shortcut_lines = "\n".join(
                f"- `{self.shortcut(action_id)}` — {labels.get(action_id, action_id)} (`{action_id}`)"
                for action_id, _key in sorted(self.keybindings.items())
            )
            navigation_help = self.tr(
                "- `{0}`: Sol paneli aç ve odağı al\n- `{1}` / `{2}`: proje ve sohbetlerde gezin; `Enter` sohbeti açar veya grubu değiştirir\n- `{3}`: seçili proje/bölümü daralt veya genişlet\n- `{4}`: yüklenmiş sohbetleri ara; `Enter` seçili sohbeti açar\n- `{5}`: sol paneli gizle/göster\n- `{6}`: son 50 sohbeti ara; `Enter` seçileni sekmede açar\n- `{7}` yeni sekme; `{8}` / `{9}` sekme değiştirir; `{10}` sekmeyi kapatır\n- Sekmede `●` yanıtın sürdüğünü, `✓` yanıtın tamamlandığını gösterir\n- Yanıt sürerken açık sekmelere geçilebilir; uyumluluk daemon'ı seri çalıştığı için başka sohbet yükleme/gönderme bekler\n"
            ).format(
                self.shortcut("navigation.focus_sidebar"),
                self.shortcut("navigation.sidebar_up"),
                self.shortcut("navigation.sidebar_down"),
                self.shortcut("navigation.sidebar_toggle_group"),
                self.shortcut("ui.search"),
                self.shortcut("ui.toggle_sidebar"),
                self.shortcut("chat.resume"),
                self.shortcut("chat.new"),
                self.shortcut("chat.previous_tab"),
                self.shortcut("chat.next_tab"),
                self.shortcut("chat.close_tab"),
            )
            self.append_system(
                self.text(
                    "help_body",
                    f"**Commands**\n\n- `/new` start a conversation\n- `/tab-next` or `/tab-prev` switch tabs\n- `/tab-close` close the current tab\n- `/search` search saved conversations\n- `/projects` list projects; `/proje` or `/project [ID]` move this chat or choose its project; `/project info` opens details\n- `/temporary` or `/private` starts a temporary chat without local conversation files\n- `/model [MODEL_ID]` choose from the account's model catalog\n- `/effort [ID|default]` choose an effort offered by the selected model\n- `/refresh` refresh models, projects and chats (F10 by default)\n- `/actions` open searchable actions and commands\n- `/settings` opens categorized settings and accounts in a workspace tab\n- `/action ID` run an application action\n- `/resume` search recent conversations\n- `/resume ID` open a conversation directly\n- `/history` reload this conversation\n- `/rename TITLE` rename this conversation\n- `/delete [ID]` delete a chat; `/project-delete [ID]` delete a project and its contents (confirmation required)\n- `/copy` copy the latest assistant response\n- `/remove` remove the latest pending image\n- `/clear` clear the displayed transcript\n- `/exit` or `/quit` quit\n\n**Text editing**\n\nCtrl+A selects composer text; Ctrl+C copies a selection; Ctrl+V pastes. Click a message, then Ctrl+A / Ctrl+C to copy that message.\nTemporary chat uses the provider history-disabled request; provider retention rules still apply.\n\n\n**Navigation**\n\n{navigation_help}\n**Configured shortcuts**\n\n{shortcut_lines}",
                ).format(navigation=navigation_help, shortcuts=shortcut_lines)
            )
            self.messages[-1]["local_help"] = True
        else:
            self.status = self.tr("Unknown command: {0}").format(name)
            self.app.invalidate()

    def rename_current_conversation(self, title: str) -> None:
        title = title.strip()
        if self.conversation_id is None:
            self.status = self.tr("Save the conversation before renaming it")
            self.app.invalidate()
            return
        if not title:
            self.status = self.tr("Usage: /rename TITLE")
            self.app.invalidate()
            return
        if self.rename_conversation is None:
            self.status = self.tr("Conversation rename is unavailable")
            self.app.invalidate()
            return
        conversation_id = self.conversation_id
        target_tab = self.active_tab
        target_state = self.tabs[self.active_tab]
        self.status = self.tr("Renaming conversation…")
        self.background_jobs += 1
        self.app.invalidate()

        def rename() -> None:
            error_text = None
            try:
                self.rename_conversation(conversation_id, title)
            except Exception as error:
                error_text = str(error)

            def apply_result() -> None:
                self.background_jobs = max(0, self.background_jobs - 1)
                if error_text is None:
                    target_state["title"] = title
                if target_state is self.tabs[self.active_tab]:
                    if error_text is None:
                        self.title = title
                        self._save_active_tab()
                        self.status = self.tr("Conversation renamed")
                    else:
                        self.status = self.tr("Rename failed: {0}").format(error_text)
                else:
                    self.status = (
                        self.tr("Rename failed in tab {0}: {1}").format(target_tab + 1, error_text)
                        if error_text
                        else self.tr("Rename completed in tab {0}").format(target_tab + 1)
                    )
                self.app.invalidate()

            self._dispatch_ui(apply_result)

        if not self._operations.submit(rename):
            self.background_jobs = max(0, self.background_jobs - 1)
            self.status = self.tr("Arka plan iş kuyruğu dolu; yeniden deneyin")

    def paste_clipboard_image(self) -> None:
        if self.uploading_image:
            self.status = self.tr("An image upload is already in progress")
            self.app.invalidate()
            return
        if self.upload_image is None:
            self.status = self.tr("Image upload is unavailable")
            self.app.invalidate()
            return
        upload_image = self.upload_image
        source_account_key = self.account_key
        source_tab_id = self.tabs[self.active_tab].get("tab_id")
        self.uploading_image = True
        self.background_jobs += 1
        self.upload_started = time.monotonic()
        self.status = self.tr("Reading image from clipboard…")
        self.app.invalidate()

        def upload() -> None:
            try:
                data = clipboard.read_png()
                if len(data) < 24:
                    raise RuntimeError("clipboard PNG is truncated")
                width = int.from_bytes(data[16:20], "big")
                height = int.from_bytes(data[20:24], "big")
                if width < 1 or height < 1:
                    raise RuntimeError("clipboard PNG has invalid dimensions")
                name = datetime.now().strftime("clipboard-%Y%m%d-%H%M%S.png")
                attachment = upload_image(data, name, "image/png", width, height)
                if not isinstance(attachment, dict) or not attachment.get("id"):
                    raise RuntimeError("image upload returned no file ID")
                message = None
            except Exception as error:
                attachment = None
                message = str(error)
            finally:

                def finish_upload() -> None:
                    self.background_jobs = max(0, self.background_jobs - 1)
                    self.uploading_image = False
                    self.upload_started = None
                    if attachment is not None:
                        queued = dict(attachment)
                        queued["queue_id"] = uuid.uuid4().hex
                        queued["source_account_key"] = source_account_key
                        queued["source_tab_id"] = source_tab_id
                        queued.setdefault("size_bytes", len(data))
                        self.pending_attachments.append(queued)
                        self.status = self.tr("{0} dosya eklendi").format(1)
                    else:
                        self.status = self.tr("Image paste failed: {0}").format(message)
                    self.app.invalidate()

                self._dispatch_ui(finish_upload)

        if not self._operations.submit(upload):
            self.background_jobs = max(0, self.background_jobs - 1)
            self.uploading_image, self.upload_started = False, None
            self.status = self.tr("Arka plan iş kuyruğu dolu; yeniden deneyin")

    def copy_latest_response(self) -> None:
        with self.lock:
            response = next(
                (
                    message["text"]
                    for message in reversed(self.messages)
                    if message["role"] == "assistant" and message["text"]
                ),
                "",
            )
        if not response:
            self.status = self.tr("No assistant response to copy")
            self.app.invalidate()
            return

        response = render_chatgpt_annotations(response)
        if clipboard.copy_text(response):
            self.status = self.tr("Copied latest response")
            self.app.invalidate()
            return
        self.app.output.write_raw(clipboard.terminal_sequence(response))
        self.app.output.flush()
        self.status = self.tr("Sent latest response to the terminal clipboard")
        self.app.invalidate()

    def open_resume(self) -> None:
        if self.palette_visible:
            self.close_overlays()
        if self.loading_conversation or self.resume_visible:
            return
        self.resume_visible = True
        self.resume_loading = True
        self.resume_items = []
        self.resume_filtered = []
        self.resume_index = 0
        self.resume_search.text = ""
        self.status = self.tr("Loading recent conversations…")
        self.app.layout.focus(self.resume_search)
        self.app.invalidate()

        epoch = self.session_epoch

        def load_items() -> None:
            try:
                items = self.list_conversations()

                def apply_items() -> None:
                    if epoch != self.session_epoch:
                        return
                    self.resume_items = items
                    self.resume_loading = False
                    self.filter_resume()
                    self.status = f"{len(items)} recent conversations"
                    self.app.invalidate()

                self._dispatch_ui(apply_items)
            except Exception as error:
                message = str(error)

                def show_error() -> None:
                    if epoch != self.session_epoch:
                        return
                    self.resume_loading = False
                    self.status = self.tr("Conversation list failed: {0}").format(message)
                    self.app.invalidate()

                self._dispatch_ui(show_error)

        if not self._reads.submit(load_items):
            self.resume_loading = False
            self.status = self.tr("Arka plan iş kuyruğu dolu; yeniden deneyin")

    def filter_resume(self) -> None:
        query = self.resume_search.text.strip().casefold()
        self.resume_filtered = [
            item
            for item in self.resume_items
            if query in str(item.get("title") or self.tr("Untitled")).casefold()
            or query in str(item.get("id") or "").casefold()
        ]
        self.resume_index = min(self.resume_index, max(0, len(self.resume_filtered) - 1))
        self.resume_list_window.vertical_scroll = max(0, self.resume_index - 2)
        if hasattr(self, "app"):
            self.app.invalidate()

    def close_resume(self) -> None:
        self.resume_visible = False
        self.status = "Ready"
        self.app.layout.focus(self.input)
        self.app.invalidate()

    def select_resume(self) -> None:
        if not self.resume_filtered:
            return
        item = self.resume_filtered[self.resume_index]
        conversation_id = item.get("id")
        if not isinstance(conversation_id, str) or not conversation_id:
            return
        self.close_resume()
        self.new_tab()
        self.resume_conversation(conversation_id)

    def resume_conversation(self, conversation_id: str) -> None:
        detached = next(
            (tab for tab in self.background_tabs if tab.get("conversation_id") == conversation_id),
            None,
        )
        if detached is not None:
            self._save_active_tab()
            self.background_tabs = [tab for tab in self.background_tabs if tab is not detached]
            self.tabs.append(detached)
            self.active_tab = len(self.tabs) - 1
            self._restore_active_tab()
            return
        if self.temporary:
            count = len(self.tabs)
            self.new_tab()
            if len(self.tabs) == count:
                return
        if self.loading_conversation:
            self.status = self.tr("Yükleme tamamlanınca başka bir sohbet açabilirsiniz")
            self.app.invalidate()
            return
        if self.active_busy():
            # Hydrate another chat in a fresh state; the streaming state stays intact.
            self.new_tab()
            if self.active_busy():
                return
        target_tab = self.tabs[self.active_tab]
        if self.account_service:
            key, remote = origin(conversation_id, self.account_key)
            if key != self.account_key:
                self.bind_account(key)
            self.model, self.effort = "", None
            self.restore_account_preference()
            target_tab.update(
                account_key=key,
                catalog=self.catalog,
                model=self.model,
                effort=self.effort,
                effort_preference=self.effort_preference,
                temporary=False,
                kind="chat",
            )
            cached = None
        load_callback = self.load_conversation
        target_tab.pop("history_window", None)
        target_tab.pop("history_read", None)
        token = uuid.uuid4().hex
        self.loading_conversation = True
        self.loading_started = time.monotonic()
        self.status = self.tr("Loading conversation…")
        target_tab.update(loading=True, loading_started=self.loading_started, load_token=token)
        if self.account_service:
            # The provider resolves its remote parent/context itself. UI history
            # hydration does not have to block typing or a send to a known ID.
            row = next((x for x in self.sidebar_items if x.get("id") == conversation_id), {})
            project_id = row.get("project_id")
            project = dict(self.project_items.get(project_id, {}))
            if project_id:
                project["id"] = project_id
            target_tab.update(
                conversation_id=conversation_id,
                messages=target_tab["messages"] if cached is not None else [],
                title=target_tab["title"]
                if cached is not None
                else row.get("title") or self.tr("Sohbet"),
                project=target_tab.get("project", {}) if cached is not None else project,
                loading=False,
                syncing=True,
            )
            self._restore_active_tab(focus=False)
            self.status = self.tr("Geçmiş arka planda yükleniyor · mesaj yazabilirsiniz")
        self.app.layout.focus(self.input)
        self.app.invalidate()

        def load_selected(use_cache=True) -> None:
            try:
                cached = (
                    self.account_service.cached_window(key, remote)
                    if self.account_service and use_cache
                    else None
                )
                conversation = cached if cached is not None else load_callback(conversation_id)
                if target_tab.get("load_token") != token or not any(
                    tab is target_tab for tab in self.tabs
                ):
                    return
                messages = []
                for message in conversation.get("messages", []):
                    visible = visible_chat_message(message)
                    if visible is not None:
                        messages.append(visible)

                def apply_conversation() -> None:
                    if (
                        not any(tab is target_tab for tab in self.tabs)
                        or target_tab.get("load_token") != token
                    ):
                        return
                    if target_tab is self.tabs[self.active_tab]:
                        self._save_active_tab()
                    if target_tab.get("busy"):
                        # Account persistence will append to the hydrated archive.
                        # Never replace the composer/stream while it is running.
                        target_tab["syncing"] = False
                        self.app.invalidate()
                        return
                    visible_messages = messages
                    visible_messages = merge_conversation(
                        {"messages": visible_messages}, {"messages": target_tab.get("messages", [])}
                    )["messages"]
                    project = conversation.get("project")
                    project = dict(project) if isinstance(project, dict) else {}
                    project_id = conversation.get("project_id") or project.get("id")
                    project = dict(self.project_items.get(project_id, {})) | project
                    if project_id:
                        project["id"] = project_id
                    if conversation.get("project_name"):
                        project["name"] = conversation["project_name"]
                    target_tab.update(
                        messages=visible_messages,
                        history_window=conversation.get("history_window"),
                        conversation_id=conversation_id,
                        title=conversation.get("title") or self.tr("Untitled"),
                        project=project,
                        loading=False,
                        loading_started=None,
                        load_error=None,
                        syncing=cached is not None and self.archive_service() is not None,
                    )
                    model = conversation.get("model") or conversation.get("default_model_slug")
                    if isinstance(model, str) and model:
                        if self.account_service:
                            target_tab["response_model"] = model
                        else:
                            target_tab["model"] = model
                    if target_tab is self.tabs[self.active_tab]:
                        self._restore_active_tab(focus=False)
                        previous = getattr(self, "_virtual_transcript", None)
                        if previous is not None:
                            previous.dispose()
                        self._virtual_transcript = None
                        self._virtual_signature = None
                        self._virtual_tab_id = target_tab.get("tab_id")
                        self.status = (
                            self.tr("Kayıtlı geçmiş · arka planda eşitleniyor")
                            if cached is not None
                            else "Ready"
                        )
                        self.scroll_bottom()
                    else:
                        self.app.invalidate()

                self._dispatch_ui(apply_conversation)
                if cached is not None and self.archive_service():
                    self._read_in_background(
                        lambda: load_callback(conversation_id),
                        lambda _: self.apply_archive_updates(),
                        lambda reason: setattr(
                            self, "status", self.tr("Senkronizasyon: ") + reason
                        ),
                        session_bound=False,
                    )
                elif cached is not None:
                    load_selected(False)
            except Exception as error:
                message = str(error)

                def show_error() -> None:
                    if (
                        not any(tab is target_tab for tab in self.tabs)
                        or target_tab.get("load_token") != token
                    ):
                        return
                    target_tab.update(
                        loading=False, syncing=False, loading_started=None, load_error=message
                    )
                    if target_tab is self.tabs[self.active_tab]:
                        self.loading_conversation = False
                        self.loading_started = None
                        self.status = self.tr("Load failed: {0}").format(message)
                    self.app.invalidate()

                self._dispatch_ui(show_error)

        if not self._reads.submit(load_selected):
            target_tab.update(loading=False, syncing=False, loading_started=None)
            self.loading_conversation, self.loading_started = False, None
            self.status = self.tr("Arka plan iş kuyruğu dolu; yeniden deneyin")

    def run_send(
        self,
        prompt: str,
        attachments: list[dict[str, Any]],
        tab_index: int,
        messages: list[Message],
        conversation_id: str | None,
        model: str,
        effort: str | None,
        project_id: str | None = None,
        temporary: bool = False,
        send_callback=None,
        target_tab=None,
    ) -> None:
        target_tab = self.tabs[tab_index] if target_tab is None else target_tab

        def is_active():
            return target_tab is self.tabs[self.active_tab]

        rendered = ""
        stream_id = uuid.uuid4().hex
        started = target_tab.get("started", self.active_started)
        final_message_id = None
        elapsed = None
        final_status = "Ready"
        send_succeeded = False
        submitted_attachment_ids = {
            str(item["queue_id"]) for item in attachments if item.get("queue_id")
        }
        resolved_id = conversation_id
        resolved_title: str | None = None
        resolved_model: str | None = None
        refresh_lock = threading.Lock()
        refresh_pending = False
        refresh_status = False

        def schedule_stream_refresh(*, show_status: bool = False) -> None:
            nonlocal refresh_pending, refresh_status
            with refresh_lock:
                refresh_status = refresh_status or show_status
                if refresh_pending:
                    return
                refresh_pending = True

            def update_active_view() -> None:
                nonlocal refresh_pending, refresh_status
                with refresh_lock:
                    update_status = refresh_status
                    refresh_status = False
                    refresh_pending = False
                if is_active():
                    if update_status:
                        self.status = self.tr("Streaming response…")
                    self.scroll_bottom(force=False)

            self._dispatch_ui(update_active_view)

        def register_connection(connection: Any) -> None:
            target_tab["connection"] = connection
            target_tab["job_id"] = getattr(connection, "id", None)
            if is_active():
                self.cancel_connection = connection
            if target_tab.get("cancel_requested"):
                self._cancel_connection(connection)

        def on_event(event: dict[str, Any]) -> None:
            nonlocal rendered, final_message_id
            connection = target_tab.get("connection")
            if connection is not None:
                target_tab["job_id"] = getattr(connection, "id", None)
            event_type = event.get("type")
            if event.get("channel") == "analysis" or event_type == "reasoning":
                text = event.get("text")
                if not isinstance(text, str):
                    return
                identity = stream_id + ":" + str(event.get("message_id") or "reasoning")
                with self.lock:
                    item = next(
                        (item for item in messages if item.get("stream_id") == identity), None
                    )
                    if item is None:
                        item = {
                            "role": "reasoning",
                            "text": "",
                            "stream_id": identity,
                            "local_pending": True,
                        }
                        messages.insert(max(0, len(messages) - 1), item)
                    item["text"] = item["text"] + text if event_type == "delta" else text
                    target_tab["render_revision"] = target_tab.get("render_revision", 0) + 1
                schedule_stream_refresh()
                return
            identity = event.get("message_id")
            if identity and final_message_id and identity != final_message_id:
                rendered = ""
            if identity:
                final_message_id = identity
            text = event.get("text")
            if event_type == "delta" and isinstance(text, str):
                rendered += text
            elif event_type == "replace" and isinstance(text, str):
                rendered = text
            else:
                return
            with self.lock:
                messages[-1]["text"] = rendered
                target_tab["render_revision"] = target_tab.get("render_revision", 0) + 1
            schedule_stream_refresh(show_status=True)

        try:
            result = (send_callback or self.send_prompt)(
                conversation_id,
                prompt,
                attachments,
                on_event,
                register_connection,
                model,
                effort,
                **({"project_id": project_id} if project_id else {}),
                **({"temporary": True} if temporary else {}),
            )
            send_succeeded = True
            omitted_count = int(result.get("attachment_omitted_count") or 0)
            if omitted_count:
                final_status = self.tr(
                    "Yanıt tamamlandı · {0} dosya gönderilemedi ve listeden kaldırıldı"
                ).format(omitted_count)
            if result.get("cache_warning"):
                final_status = result["cache_warning"]
            result_id = result.get("conversation_id")
            if isinstance(result_id, str) and result_id:
                resolved_id = result_id
            conversation = result.get("conversation")
            if isinstance(conversation, dict):
                resolved_title = conversation.get("title") or None
                response_model = conversation.get("default_model_slug")
                if isinstance(response_model, str) and response_model:
                    resolved_model = response_model
                previous_ids = set(result.get("previous_ids", []))
                final_text = ""
                for message in conversation.get("messages", []):
                    if not isinstance(message, dict) or message.get("id") in previous_ids:
                        continue
                    author = message.get("author") or {}
                    if author.get("role") == "assistant" and message.get("channel") == "analysis":
                        from providers.chatgpt.formats import normalize_message

                        visible = visible_chat_message(normalize_message(message))
                        if visible and not any(
                            item.get("role") == "reasoning" and item.get("text") == visible["text"]
                            for item in messages
                        ):
                            with self.lock:
                                messages.insert(max(0, len(messages) - 1), visible)
                        continue
                    if author.get("role") != "assistant" or message.get("recipient") not in (
                        None,
                        "all",
                    ):
                        continue
                    metadata = message.get("metadata") or {}
                    if isinstance(metadata, dict) and isinstance(metadata.get("model_slug"), str):
                        resolved_model = metadata["model_slug"]
                    content = message.get("content") or {}
                    parts = content.get("parts") or []
                    text_parts = [part for part in parts if isinstance(part, str)]
                    if text_parts:
                        final_text = "\n".join(text_parts)
                if final_text and final_text != rendered:
                    with self.lock:
                        messages[-1]["text"] = final_text
        except Exception as error:
            if not rendered:
                with self.lock:
                    messages[-1]["text"] = "*" + self.tr("Request stopped: {0}").format(error) + "*"
            final_status = self.tr("Response stopped")
        finally:
            if started is not None:
                elapsed = time.monotonic() - started

            def finish_turn() -> None:
                if attachments:
                    submitted_ids = submitted_attachment_ids
                    self.pending_attachments = [
                        item
                        for item in self.pending_attachments
                        if str(item.get("queue_id") or "") not in submitted_ids
                    ]
                    if send_succeeded:
                        self._release_staged_attachments(attachments)
                    else:
                        queued_ids = {
                            str(item.get("queue_id") or "") for item in self.pending_attachments
                        }
                        restored = [
                            item
                            for item in attachments
                            if str(item.get("queue_id") or "") not in queued_ids
                        ]
                        self.pending_attachments = restored + self.pending_attachments
                    self._attachments_in_flight = False
                    self._inflight_attachments = []
                tab = target_tab
                tab["temporary"] = temporary
                tab["conversation_id"] = resolved_id
                tab["title"] = resolved_title or tab.get("title") or self.tr("New conversation")
                tab["messages"] = messages
                tab["model"] = model or resolved_model or ""
                tab["response_model"] = resolved_model
                tab["effort"] = effort
                tab["busy"] = False
                tab["completed_pending"] = not is_active()
                tab["started"] = None
                tab["connection"] = None
                tab["cancel_requested"] = False
                if resolved_id and not temporary:
                    record = next(
                        (item for item in self.sidebar_items if item.get("id") == resolved_id), None
                    )
                    if record is None:
                        record = {"id": resolved_id}
                        self.sidebar_items.insert(0, record)
                    record.update(
                        title=tab["title"],
                        provider_id=tab.get("account_key", self.account_key).split("/")[0],
                        project_id=(tab.get("project") or {}).get("id"),
                    )
                self._invalidate_sidebar_rows()
                if (
                    any(item is tab for item in self.background_tabs)
                    and send_succeeded
                    and (temporary or self.archive_service())
                ):
                    if temporary or not result.get("cache_warning"):
                        self.background_tabs = [
                            item for item in self.background_tabs if item is not tab
                        ]
                        tab["messages"] = []
                        if temporary and resolved_id:
                            forget = (
                                self.account_service.callbacks(
                                    tab.get("account_key", self.account_key)
                                ).get("forget_temporary")
                                if self.account_service
                                else self.forget_temporary
                            )
                            if forget:
                                self._read_in_background(
                                    lambda: forget(resolved_id),
                                    lambda _: None,
                                    lambda _: None,
                                    session_bound=False,
                                )
                        if self.archive_service() and resolved_id:
                            key, remote = origin(
                                resolved_id, tab.get("account_key", self.account_key)
                            )
                            self.archive_service().release(key, remote)
                self._sync_activity()
                if is_active():
                    self.stop_confirmation_until = 0.0
                    self.stop_confirmation_tab_id = None
                    self.conversation_id = resolved_id
                    self.title = tab["title"]
                    self.messages = messages
                    self.model = tab["model"]
                    self.last_response_model = resolved_model
                    self.effort = effort
                    self.last_elapsed = elapsed
                    self.status = final_status
                    if self.set_active_conversation is not None:
                        self.set_active_conversation(None if tab.get("temporary") else resolved_id)
                    self._save_active_tab()
                    self.scroll_bottom(force=False)
                    if send_succeeded and not temporary and self.archive_service():
                        tab.setdefault("history_window", {"start": 0, "end": 0, "total": 0})
                        self.history_pager.request("bottom", refresh=True)
                else:
                    self.status = self.tr("Yanıt tamamlandı: {0}").format(tab["title"])
                    self.app.invalidate()

            self._dispatch_ui(finish_turn)

    def run(self) -> None:
        def started() -> None:
            self.event_loop = asyncio.get_running_loop()
            self.app.create_background_task(self.animate_activity())
            if self.load_preferences:
                self._read_in_background(
                    self.load_preferences,
                    self.apply_preferences,
                    lambda reason: setattr(self, "status", reason),
                    session_bound=False,
                )
            self.refresh_provider_catalog()
            self.refresh_sidebar()
            if self.load_on_start and self.conversation_id is not None:
                self.resume_conversation(self.conversation_id)
            else:
                self.refresh_open_conversation()

        try:
            from prompt_toolkit.patch_stdout import patch_stdout

            with patch_stdout(raw=True):
                self.app.run(pre_run=started)
        finally:
            self._reads.close()
            self._render_reads.close()
            self._operations.close()
            if self.forget_temporary and not self.busy:
                self._save_active_tab()
                for tab in self.tabs:
                    if tab.get("temporary") and tab.get("conversation_id"):
                        try:
                            self.forget_temporary(tab["conversation_id"])
                        except Exception:
                            pass
            if self.action_cleanup:
                self.action_cleanup()


def run_tui(
    list_conversations: ListConversations,
    load_conversation: LoadConversation,
    send_prompt: SendPrompt,
    conversation_id: str | None = None,
    title: str = "New conversation",
    initial_messages: list[dict[str, Any]] | None = None,
    model: str | None = None,
    upload_image: UploadImage | None = None,
    rename_conversation: RenameConversation | None = None,
    set_active_conversation: SetActiveConversation | None = None,
    keybindings: dict[str, str] | None = None,
    list_workspaces: Callable[[], list[dict[str, str]]] | None = None,
    list_actions: Callable[[], list[dict[str, str]]] | None = None,
    invoke_action: Callable[..., Any] | None = None,
    effort_preference: str = "medium",
    load_catalog: Callable[[], ModelCatalog] | None = None,
    list_projects: Callable[..., dict[str, Any]] | None = None,
    get_project: Callable[[str], dict[str, Any]] | None = None,
    list_project_chats: Callable[..., dict[str, Any]] | None = None,
    initial_project: dict[str, Any] | None = None,
    refresh_on_start: bool = False,
    **callbacks,
) -> None:
    view = ChatView(
        list_conversations,
        load_conversation,
        send_prompt,
        conversation_id,
        title,
        initial_messages,
        model,
        upload_image,
        rename_conversation,
        set_active_conversation,
        keybindings,
        list_workspaces,
        list_actions,
        invoke_action,
        effort_preference,
        load_catalog,
        list_projects,
        get_project,
        list_project_chats,
        initial_project,
        refresh_on_start,
        **callbacks,
    )
    initial_messages = None
    view.run()

"""ChatView composition and fullscreen TUI lifecycle."""

from __future__ import annotations

import asyncio
import threading
import uuid
from typing import Any, Callable

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import DynamicKeyBindings
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.margins import ConditionalMargin, ScrollbarMargin
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea

from providers import Capability
from providers.models import FeatureState, ModelCatalog
from settings import DEFAULT_KEYBINDINGS

from ..bindings import key_bindings
from ..icons import Icon
from ..layout import render_tab_bar, workspace_layout
from ..markdown import (
    MessageRenderer,
    visible_chat_messages,
)
from ..projects import ProjectPanel
from ..session import SessionDialog
from ..settings import SettingsDialog
from ..styles import (
    DEFAULT_TERMINAL_STYLE,
)
from ..widgets import (
    BoundedTextControl,
    ComposerPlaceholderProcessor,
    SidebarControl,
    SlashCommandCompleter,
    TranscriptControl,
    TranscriptGutter,
    TranscriptWindow,
)
from .accounts import AccountComponent
from .archive import ArchiveComponent
from .commands import CommandComponent
from .composer import ComposerComponent
from .contracts import (
    ListConversations,
    LoadConversation,
    Message,
    RenameConversation,
    SendPrompt,
    SetActiveConversation,
    UploadImage,
)
from .controls import ControlsComponent
from .history import HistoryComponent
from .messages import MessageComponent
from .models import ModelComponent
from .preferences import PreferencesComponent
from .projects import ProjectComponent
from .sending import SendingComponent
from .sidebar import SidebarComponent
from .tabs import TabComponent
from .transcript import TranscriptComponent


class ChatView(
    ArchiveComponent,
    AccountComponent,
    ControlsComponent,
    PreferencesComponent,
    SidebarComponent,
    ModelComponent,
    ProjectComponent,
    CommandComponent,
    TabComponent,
    TranscriptComponent,
    ComposerComponent,
    MessageComponent,
    HistoryComponent,
    SendingComponent,
):
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
        list_shortcut_actions=None,
    ) -> None:
        from ..widgets import install_keyboard_protocol, register_modified_enter

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
        self._sidebar_capabilities_cache = {}
        self._model_picker_requests = set()
        self.attach_file_callback = None
        self.attachment_policy_callback = None
        self.settings_rows_callback = settings_rows
        self.change_setting = change_setting
        self.load_preferences = load_preferences
        self.session_store = None
        self.session_restored = False
        self._restoring_session = False
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
        self._history_page_token = 0
        self._history_seen_cursors: set[str] = set()
        self.list_conversations = list_conversations
        self.load_conversation = load_conversation
        self.send_prompt = send_prompt
        self.upload_image = upload_image
        self.rename_conversation = rename_conversation
        self.set_active_conversation = set_active_conversation
        self.keybindings = dict(DEFAULT_KEYBINDINGS) | (keybindings or {})
        self.list_workspaces = list_workspaces or (lambda: [])
        self.list_actions = list_actions or (lambda: [])
        self.list_shortcut_actions = list_shortcut_actions or self.list_actions
        self.invoke_action = invoke_action
        self.conversation_id = conversation_id
        self.title = title
        self.model = model or ""
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
        self._project_chat_pages_loading: set[tuple[str, str]] = set()
        self._project_chat_page_ids: dict[tuple[str, str], set[str]] = {}
        self._project_chat_refreshes: dict[str, int] = {}
        self.project_chat_cursors: dict[str, str | None] = {}
        self._project_seen_cursors: set[str] = set()
        self._project_chat_seen_cursors: dict[str, set[str]] = {}
        self.project_chat_errors: dict[str, str] = {}
        self.selected_message = 0
        self.message_selection = False
        self.show_reasoning = True
        self.message_ranges = []
        self.temporary = False
        self.tab_kind = "chat"
        self.last_response_model: str | None = None
        self.current_project: dict[str, Any] = dict(initial_project or {})
        self.messages: list[Message] = visible_chat_messages(initial_messages or [])
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
        self._temporary_status_handle = None
        self._temporary_status_text = None
        self._temporary_status_restore = None
        self.cancel_connection: Any = None
        self.stop_confirmation_until = 0.0
        self.stop_confirmation_tab_id: str | None = None
        from ..background import BackgroundReads

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
        from ..paging import HistoryPager

        self.history_pager = HistoryPager(self)
        from ..transcript import TranscriptPrefetch

        self._transcript_prefetch = TranscriptPrefetch(
            lambda read, ready, failed: self._read_in_background(
                read, ready, failed, session_bound=False, cpu=True
            ),
            lambda: self.app.invalidate(),
        )
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
        self.transcript_gutter = TranscriptGutter(self)
        self.transcript_window = TranscriptWindow(
            get_content_height=lambda: getattr(
                getattr(self, "_virtual_transcript", None), "line_count", None
            ),
            content=self.transcript_control,
            wrap_lines=False,
            get_line_prefix=self.transcript_gutter,
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
            height=1,
            prompt=self.tr(" Ara "),
            style="class:sidebar.search",
            focus_on_click=True,
        )
        self.sidebar_search.buffer.on_text_changed += lambda _buffer: self.filter_sidebar()
        self.sidebar_control = SidebarControl(self)
        self.sidebar_window = Window(
            content=self.sidebar_control, wrap_lines=False, always_hide_cursor=True
        )
        self.palette_search = TextArea(
            height=1, prompt=f"{Icon.SEARCH} ", style="class:palette.search"
        )
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
        from ..forms import Forms

        self.forms = Forms(self)
        self.project_panel = ProjectPanel(self)
        self.session_dialog = SessionDialog(self)
        self.settings_dialog = SettingsDialog(self)
        from ..downloads import DownloadsPanel

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

    def tr(self, source):
        from language import literal

        return literal(self.translations, source)

    def text(self, key, fallback):
        return self.translations.get(f"application.{key}", fallback)

    def render_prompt(self) -> FormattedText:
        return FormattedText([("class:prompt", " › ")])

    def render_tabs(self):
        return render_tab_bar(self)

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
            if self.account_service and hasattr(self.account_service, "prepare_enabled"):

                def show_warmup_failures(failures):
                    reason = failures.get(self.account_key)
                    if reason and not self.active_busy() and not self.catalog_loading:
                        self.status = self.tr("Sağlayıcı bağlantısı hazırlanamadı: {0}").format(
                            reason
                        )
                        self.app.invalidate()

                self._read_in_background(
                    self.account_service.prepare_enabled,
                    show_warmup_failures,
                    lambda _: None,
                    session_bound=False,
                )
            self.refresh_provider_catalog()
            self.refresh_other_provider_catalogs()
            self.refresh_sidebar()
            if self.session_restored:
                self._hydrate_restored_tab(self.tabs[self.active_tab])
            elif self.load_on_start and self.conversation_id is not None:
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
            self._persist_session()
            if self.forget_temporary and not self.busy:
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
    initial_session: dict | None = None,
    session_store=None,
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
    view.session_store = session_store
    if initial_session is not None:
        view.restore_session(initial_session)
    initial_messages = None
    view.run()

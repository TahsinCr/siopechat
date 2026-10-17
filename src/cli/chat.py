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
from prompt_toolkit.formatted_text import ANSI, FormattedText
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.layout.margins import ConditionalMargin, ScrollbarMargin
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import TextArea
from rich.console import Console
from rich.text import Text

from accounts import origin
from providers import Capability
from providers.models import FeatureState, ModelCatalog
from settings import DEFAULT_KEYBINDINGS
from utils import clipboard

from .actions import action_order, action_title
from .bindings import key_bindings
from .layout import workspace_layout
from .markdown import (
    MessageRenderer,
    render_chatgpt_annotations,
    visible_chat_message,
)
from .session import SessionDialog
from .settings import SettingsDialog
from .styles import DEFAULT_TERMINAL_STYLE, terminal_style
from .widgets import (
    ComposerPlaceholderProcessor,
    SlashCommandCompleter,
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
        from .widgets import register_modified_enter

        register_modified_enter()
        self.account_service = account_service
        self.account_key = account_service.active() if account_service else "chatgpt/default"
        self.provider_id = self.account_key.split("/")[0]
        self.provider_features = frozenset({Capability.PROJECTS})
        self.provider_filter = None
        self._account_projects = {}
        self._account_catalogs = {}
        self._catalog_requests = {}
        self._project_requests = {}
        self.attach_file_callback = None
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
        self.send_mode = os.environ.get("CHATGPT_WEB_SEND_MODE", "node")
        self.current_project: dict[str, Any] = dict(initial_project or {})
        self.messages: list[Message] = []
        for message in initial_messages or []:
            visible = visible_chat_message(message)
            if visible is not None:
                self.messages.append(visible)
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
        self.status = "Ready"
        self.cancel_connection: Any = None
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
        self.formatted_transcript_source = ""
        self.formatted_transcript: ANSI = ANSI("")
        self.event_loop: asyncio.AbstractEventLoop | None = None
        self.sidebar_items: list[dict[str, Any]] = []
        self.sidebar_index = 0
        self.sidebar_error: str | None = None
        self.workspace_error: str | None = None
        self.sidebar_loading = False
        self.sidebar_refresh_generation = 0
        self.sidebar_visible = True
        self.sidebar_collapsed: set[str] = set()
        self.sidebar_rows: list[dict[str, Any]] = []
        self.palette_visible = False
        self.palette_mode = "actions"
        self.palette_query = ""
        self.palette_items: list[dict[str, str]] = []
        self.palette_filtered: list[dict[str, str]] = []
        self.palette_index = 0
        self.sidebar_visible_filter = Condition(lambda: self.sidebar_visible)
        self.palette_filter = Condition(lambda: self.palette_visible)
        self.tabs: list[dict[str, Any]] = [dict(self._new_tab_state(), tab_id=uuid.uuid4().hex)]
        self.active_tab = 0

        self.transcript_control = FormattedTextControl(
            text=self.render_formatted_transcript,
            focusable=True,
            get_cursor_position=self.transcript_cursor_position,
        )
        self.transcript_window = TranscriptWindow(
            content=self.transcript_control,
            wrap_lines=True,
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
            completer=SlashCommandCompleter(),
            complete_while_typing=True,
            input_processors=[ComposerPlaceholderProcessor()],
            read_only=Condition(lambda: self.tab_kind == "project"),
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
            prompt="Search: ",
        )
        self.resume_search.buffer.on_text_changed += lambda _buffer: self.filter_resume()
        self.sidebar_search = TextArea(height=1, prompt=" Ara ", style="class:sidebar.search")
        self.sidebar_search.buffer.on_text_changed += lambda _buffer: self.filter_sidebar()
        self.sidebar_control = FormattedTextControl(
            text=self.render_sidebar,
            focusable=True,
            get_cursor_position=lambda: Point(x=0, y=self.sidebar_index),
        )
        self.sidebar_window = Window(
            content=self.sidebar_control, wrap_lines=False, always_hide_cursor=True
        )
        self.palette_search = TextArea(height=1, prompt="⌕ ", style="class:palette.search")
        self.palette_search.buffer.on_text_changed += lambda _buffer: self.filter_palette()
        self.palette_control = FormattedTextControl(
            text=self.render_palette,
            focusable=True,
            get_cursor_position=lambda: Point(x=0, y=self.palette_index),
        )
        self.palette_window = Window(
            content=self.palette_control, wrap_lines=False, always_hide_cursor=True
        )
        self.resume_list_control = FormattedTextControl(
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
        self.session_dialog = SessionDialog(self)
        self.settings_dialog = SettingsDialog(self)
        self.layout_enabled = True
        self.layout_factory = layout_factory
        self.layout_controller = layout_factory(self) if layout_factory else None
        self.bindings = key_bindings(self)
        self.app: Application[Any] = Application(
            layout=Layout(workspace_layout(self), focused_element=self.input),
            key_bindings=self.bindings,
            full_screen=True,
            mouse_support=Condition(lambda: self.mouse_enabled),
            min_redraw_interval=0.05,
            max_render_postpone_time=0.1,
            style=Style.from_dict(DEFAULT_TERMINAL_STYLE),
        )

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
        while True:
            await asyncio.sleep(0.12)
            active_tab_busy = bool(self.tabs and self.tabs[self.active_tab].get("busy"))
            if active_tab_busy or self.loading_conversation or self.uploading_image:
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
        if self.account_service and hasattr(self.account_service, "remember"):
            self.account_service.remember(self.account_key, self.model, self.effort)

    def bind_account(self, key):
        if self.account_service is None:
            return
        if key != self.account_key:
            self._account_projects[self.account_key] = self.project_items
            self.project_items = self._account_projects.get(key, {})
            self.projects_loaded = bool(self.project_items)
        self.account_key = key
        self.provider_id = key.split("/")[0]
        self.provider_features = self.account_service.capabilities(key)
        known = self._account_catalogs.get(key)
        if known is None and hasattr(self.account_service, "cached_models"):
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
        self.set_active_conversation = lambda value: self.account_service.cache(
            key
        ).set_active_conversation(origin(value, key)[1])
        self.list_conversations = lambda: self.account_service.list_chats(self.provider_filter)[
            "items"
        ]
        self.list_history_page = lambda cursor: self.account_service.list_chats(
            self.provider_filter, cursor
        )

    def select_account(self, key):
        if (
            self.account_service
            and hasattr(self.account_service, "enabled")
            and not self.account_service.enabled(key.split("/")[0])
        ):
            self.status = "Sağlayıcı devre dışı · Ayarlar → Sağlayıcılar"
            self.app.invalidate()
            return
        if (
            self.account_service is None
            or self.loading_conversation
            or self.uploading_image
            or self.session_dialog.loading
        ):
            return
        if self.tab_kind == "chat" and (self.messages or self.conversation_id or self.input.text):
            self.new_tab()
        self.bind_account(key)
        self.model = self.catalog.default_chat_model.id if self.catalog.default_chat_model else ""
        self.effort = self.catalog.effort(self.model, self.effort_preference)
        self.restore_account_preference()
        self.current_project = {}
        self.project_items = {}
        self.projects_loaded = False
        self.account_service.select(key)
        self._save_active_tab()
        self.refresh_sidebar()
        self.refresh_provider_catalog()
        self.app.invalidate()

    def add_account(self):
        if not self.account_service:
            return

        def submit(values):
            key = self.account_service.add(values["provider"], values["label"])
            self.select_account(key)
            self.settings_dialog.open("accounts")

        self.forms.open(
            "Hesap ekle",
            [
                (
                    "provider",
                    "Sağlayıcı",
                    "chatgpt",
                    [("chatgpt", "ChatGPT"), ("gemini", "Gemini")],
                ),
                ("label", "Hesap adı", "", None),
            ],
            submit,
        )

    def open_filters(self):
        if not self.account_service:
            return
        providers = sorted({account["provider"] for account in self.account_service.items()})
        if len(providers) < 2:
            return

        def apply(values):
            if not values["providers"]:
                raise ValueError("En az bir sağlayıcı seçin")
            self.provider_filter = set(values["providers"])
            self.refresh_sidebar()

        self.forms.open(
            "Sohbet filtresi",
            [
                (
                    "providers",
                    "Gösterilecek sağlayıcılar",
                    (
                        [(key, key.title()) for key in providers],
                        list(self.provider_filter or providers),
                    ),
                    None,
                )
            ],
            apply,
        )

    def open_project_create(self):
        if not self.account_service or Capability.PROJECT_CREATE not in self.provider_features:
            self.status = "Bu sağlayıcı proje oluşturmayı desteklemiyor"
            self.app.invalidate()
            return
        key = self.account_key

        def submit(values):
            if self.busy or self.background_jobs:
                raise ValueError("Devam eden işlemden sonra proje oluşturun")
            if not values["name"].strip():
                raise ValueError("Proje adı gerekir")
            self.background_jobs += 1
            self.status = "Proje oluşturuluyor…"

            def success(project):
                self.background_jobs -= 1
                self._account_projects.setdefault(key, {})[project["id"]] = project
                if self.account_key == key:
                    self.project_items[project["id"]] = project
                    self.projects_loaded = True
                    self.filter_sidebar()
                self.status = "Proje oluşturuldu · " + project["name"]
                self.app.invalidate()

            def failure(reason):
                self.background_jobs -= 1
                self.status = str(reason)
                self.app.invalidate()

            self._read_in_background(
                lambda: self.account_service.create_project(
                    key, values["name"], values["visibility"], values["memory"]
                ),
                success,
                failure,
            )

        self.forms.open(
            "Yeni proje",
            [
                ("name", "Proje adı", "", None),
                (
                    "visibility",
                    "Gizlilik",
                    "private",
                    [("private", "Özel"), ("public", "Paylaşılan")],
                ),
                (
                    "memory",
                    "Bellek",
                    "default",
                    [("default", "Varsayılan"), ("project_only", "Yalnızca proje")],
                ),
            ],
            submit,
        )

    def open_attachment(self):
        if self.attach_file_callback is None:
            self.status = "Bu sağlayıcıda dosya ekleme kullanılamıyor"
            self.app.invalidate()
            return
        callback = self.attach_file_callback

        def submit(values):
            if self.uploading_image or self.loading_conversation or self.busy:
                raise ValueError("Devam eden işlem tamamlanınca dosya ekleyin")
            self.uploading_image = True
            self.background_jobs += 1
            self.status = "Dosya hazırlanıyor…"

            def success(attachment):
                self.uploading_image = False
                self.background_jobs -= 1
                self.pending_attachments.append(attachment)
                self.status = "Dosya eklendi · " + attachment.get("name", "")
                self.app.invalidate()

            def failure(reason):
                self.uploading_image = False
                self.background_jobs -= 1
                self.status = str(reason)
                self.app.invalidate()

            self._read_in_background(lambda: callback(values["path"]), success, failure)

        self.forms.open(
            "Dosya ekle",
            [("path", "Dosya yolu · Tab ile tamamla · en fazla 20 MiB", "", None)],
            submit,
        )

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
            "pending_attachments": self.pending_attachments,
            "follow_output": self.follow_output,
            "scroll": self.transcript_window.vertical_scroll
            if hasattr(self, "transcript_window")
            else 0,
            "busy": self.active_busy(),
            "completed_pending": False,
        }

    def text(self, key, fallback):
        return self.translations.get(f"application.{key}", fallback)

    def render_tabs(self):
        width = self.terminal_width()
        prefix = fit_text(" ☰ SiopeChat ", max(0, width - 9))
        fragments = [("class:brand", prefix, on_click(self.toggle_sidebar))]
        available = max(0, width - get_cwidth(prefix) - 9)
        entries = list(range(len(self.tabs)))
        group_titles = {}
        if self.layout_controller and self.layout_enabled:
            self.layout_controller.active_group()
            for group in self.layout_controller.groups:
                members = [
                    i for i, tab in enumerate(self.tabs) if tab.get("tab_id") in group["tabs"]
                ]
                if len(members) < 2:
                    continue
                representative = self.active_tab if self.active_tab in members else members[0]
                group_titles[representative] = " ◫ ".join(
                    self.title if i == self.active_tab else self.tabs[i].get("title", "Sohbet")
                    for i in members
                )
                entries = [i for i in entries if i not in members or i == representative]
        count = min(len(entries), max(1, available // 12))
        indices = sorted(sorted(entries, key=lambda i: (abs(i - self.active_tab), i))[:count])
        base, remainder = divmod(available, max(1, len(indices)))
        for position, i in enumerate(indices):
            tab_width = min(
                getattr(self, "theme_options", {}).get("tab_width", 26),
                base + (position < remainder),
            )
            tab = self.tabs[i]
            title = group_titles.get(i) or (
                self.title if i == self.active_tab else tab.get("title", "Sohbet")
            )
            marker = (
                "● "
                if tab.get("busy")
                else "✓ "
                if tab.get("completed_pending")
                else "◌ "
                if tab.get("temporary")
                else ""
            )
            label = fit_text(marker + title, max(0, tab_width - 4))
            padding = max(0, tab_width - 4 - get_cwidth(label))
            label = " " * (padding // 2) + label + " " * (padding - padding // 2)
            style = (
                "class:tab.active"
                if i == self.active_tab
                else "class:tab.running"
                if tab.get("busy")
                else "class:tab"
            )
            fragments.append(("class:tab.divider", "│"))
            fragments.append((style, label, on_click(lambda i=i: self.select_tab(i))))
            fragments.append(
                (style + " class:tab.close", " × ", on_click(lambda i=i: self.close_clicked_tab(i)))
            )
        used = sum(get_cwidth(fragment[1]) for fragment in fragments)
        fragments.append(("class:tabs", " " * max(0, width - used - 9)))
        fragments.extend(
            [
                ("class:button", " ‹ ", on_click(lambda: self.switch_tab(-1))),
                ("class:button", " › ", on_click(lambda: self.switch_tab(1))),
                ("class:button", " + ", on_click(self.new_tab)),
            ]
        )
        return FormattedText(fragments)

    def close_clicked_tab(self, index):
        self.select_tab(index)
        if self.active_tab == index:
            self.close_tab()

    def select_tab(self, index):
        self.switch_tab(index - self.active_tab)

    def render_chat_header(self):
        if self.tab_kind == "settings":
            return FormattedText([("class:settings.title", " Ayarlar")])
        if self.tab_kind == "project":
            return FormattedText(
                [
                    (
                        "class:button",
                        " Proje bilgileri · Yenile ↻ ",
                        on_click(self.show_project_details),
                    ),
                ]
            )
        selected = next((m for m in self.catalog.chat_models if m.id == self.model), None)
        model = (
            (selected.chat_title or selected.title)
            if selected
            else self.model or ("Yükleniyor…" if self.catalog_loading else "Seçilmedi")
        )
        effort = (
            next(
                (choice.title for choice in selected.efforts if choice.id == self.effort),
                self.effort or "Sağlayıcı varsayılanı",
            )
            if selected
            else self.effort or "Sağlayıcı varsayılanı"
        )
        if selected and not selected.efforts:
            effort = "Desteklenmiyor"
        model_text = f" Model: {model} ▾ "
        effort_text = f" Efor: {effort} ▾ "
        project_text = (
            " Proje: " + fit_text(self.current_project.get("name") or "Projesiz", 22) + " ▾ "
        )
        fragments = []
        used = 0
        width = self.terminal_width()
        for style, label, callback in (
            ("class:metadata.model", model_text, lambda: self.handle_command("/model")),
            ("class:metadata.effort", effort_text, lambda: self.handle_command("/effort")),
            ("class:metadata.project", project_text, self.open_project_picker),
        ):
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

    def new_temporary_chat(self):
        count = len(self.tabs)
        self.new_tab()
        if len(self.tabs) == count:
            return
        self.temporary = True
        self.current_project = {}
        self.title = "Geçici sohbet"
        self.status = "Geçici sohbet · yerel kayıt kapalı"
        self._save_active_tab()
        self.app.invalidate()

    def open_project_picker(self):
        query = (
            self.palette_search.text
            if self.palette_visible and self.palette_mode == "projects"
            else ""
        )
        self.open_palette("projects")
        self.palette_items = [
            {
                "id": "project.select:",
                "title": "Projesiz sohbet",
                "detail": "Projeden çıkar / projesiz başlat",
            }
        ] + [
            {
                "id": "project.select:" + project_id,
                "title": item.get("name") or project_id,
                "detail": "Sohbeti taşı / yeni sohbete bağla",
            }
            for project_id, item in self.project_items.items()
        ]
        self.palette_items.append(
            {"id": "command.temporary", "title": "Yeni geçici sohbet", "detail": "Yerel kayıt yok"}
        )
        if self.project_cursor:
            self.palette_items.append(
                {"id": "project.more", "title": "Daha fazla proje…", "detail": ""}
            )
        self.palette_search.text = query
        self.filter_palette()
        if not self.projects_loaded and not self.projects_loading:
            self.refresh_projects()

    def choose_project(self, project_id):
        if project_id and project_id not in self.project_items:
            self.status = "Listeden erişilebilir bir proje seçin"
            self.app.invalidate()
            return
        if self.temporary:
            self.status = "Geçici sohbet bir projeye bağlanamaz"
            self.app.invalidate()
            return
        if self.busy or self.loading_conversation or self.background_jobs:
            self.status = "Devam eden işlem tamamlandıktan sonra proje değiştirin"
            self.app.invalidate()
            return
        if self.tab_kind != "chat":
            self.status = "Önce bir sohbet sekmesi seçin"
            self.app.invalidate()
            return
        project = dict(self.project_items[project_id]) if project_id else {}
        if not self.conversation_id:
            self.current_project = project
            self._save_active_tab()
            self.status = "Yeni sohbet · " + (project.get("name") or "Projesiz")
            self.app.invalidate()
            return
        if self.move_conversation is None:
            self.status = "Sağlayıcı bu sohbeti taşımayı desteklemiyor"
            self.app.invalidate()
            return
        remote_id = self.conversation_id
        self.background_jobs += 1
        self.status = "Proje değiştiriliyor…"
        self.app.invalidate()

        def completed(_result):
            self.background_jobs -= 1
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
            self.status = "Sohbet · " + (project.get("name") or "Projesiz")
            self.app.invalidate()

        def failed(reason):
            self.background_jobs -= 1
            self.status = "Proje değiştirilemedi: " + str(reason)
            self.app.invalidate()

        self._read_in_background(
            lambda: self.move_conversation(remote_id, project_id or None), completed, failed
        )

    def scroll_transcript(self, direction):
        self.pause_following()
        self.transcript_window.vertical_scroll = max(
            0, self.transcript_window.vertical_scroll + direction * self.transcript_page_size()
        )
        self.app.invalidate()

    def scroll_top(self):
        self.pause_following()
        self.transcript_window.vertical_scroll = 0
        self.app.invalidate()

    def render_scrollbar(self):
        return FormattedText(
            [
                ("class:button", " ↑ Önceki ", on_click(lambda: self.scroll_transcript(-1))),
                ("class:button", " ↓ Sonraki ", on_click(lambda: self.scroll_transcript(1))),
                ("class:button", " ⇣ Son mesaja git ", on_click(self.scroll_bottom)),
                (
                    "class:help",
                    " · Tekerlek / PgUp / PgDn · "
                    + ("Canlı takip" if self.follow_output else "Geçmiş okunuyor"),
                ),
            ]
        )

    def render_composer_actions(self):
        compact = self.main_width() < 42
        left = (" ◌ " if compact else " ◌ Geçici sohbet ") if self.temporary else ""
        controls = []
        if self.attach_file_callback:
            controls.append(
                (
                    "class:button",
                    " ＋ " if compact else " ＋ Dosya ",
                    on_click(self.open_attachment),
                )
            )
        if self.pending_attachments:
            controls.append(
                (
                    "class:chat.context",
                    f" {len(self.pending_attachments)} ek × ",
                    on_click(lambda: self.handle_command("/remove")),
                )
            )
        if self.layout_enabled:
            controls.append(
                ("class:button", " ◫ " if compact else " ◫ Böl ", on_click(self.begin_split))
            )
        if not self.follow_output:
            controls.append(("class:button", " ↓ ", on_click(self.scroll_bottom)))
        controls.append(
            ("class:button", " ↑ " if compact else " Gönder ↑ ", on_click(self.submit_prompt))
        )
        used = get_cwidth(left) + sum(get_cwidth(f[1]) for f in controls)
        return FormattedText(
            [
                ("class:chat.context", left),
                ("", " " * max(0, self.main_width() - used)),
            ]
            + controls
        )

    def render_footer(self):
        controls = [
            (f" {self.shortcut('command.help')} Yardım ", lambda: self.handle_command("/help")),
            (" ⚙ " + self.text("settings", "Ayarlar") + " ", self.settings_dialog.open),
            (
                " ● Hesaplar " if self.session_state.get("authenticated") else " ○ Hesaplar ",
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
            raise ValueError("Ayar bağlantısı kurulmamış")
        rows = self.settings_rows_callback()
        for row in rows:
            if row["section"] == "application" and row["key"] == "model":
                row["choices"] = ("", *(m.id for m in self.catalog.chat_models))
                row["choice_labels"] = {
                    m.id: m.chat_title or m.title for m in self.catalog.chat_models
                }
            if row["section"] == "application" and row["key"] == "effort":
                selected = next((m for m in self.catalog.chat_models if m.id == self.model), None)
                row["choices"] = tuple(e.id for e in selected.efforts) if selected else ()
                row["choice_labels"] = {e.id: e.title for e in selected.efforts} if selected else {}
        return rows

    def apply_preferences(self, preferences):
        config = preferences.get("settings", preferences)
        application = config.get("application", {})
        self.show_reasoning = application.get("show_reasoning", True)
        self.mouse_enabled = application.get("mouse", True)
        self.layout_enabled = config.get("extensions", {}).get("layout", {}).get("enabled", True)
        if self.layout_enabled and self.layout_controller is None and self.layout_factory:
            self.layout_controller = self.layout_factory(self)
        self.translations = preferences.get("translations", self.translations)
        self.keybindings = dict(DEFAULT_KEYBINDINGS) | config.get("keybindings", {})
        self.bindings = key_bindings(self)
        self.app.key_bindings = self.bindings
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
        try:
            self.app.style = terminal_style(tokens)
            self.renderer.configure(tokens)
            self.theme_options = tokens.get("options", {})
            self._transcript_signature = None
        except Exception:
            self.app.style = Style.from_dict(DEFAULT_TERMINAL_STYLE)
            self.renderer.configure({})
            self.theme_options = {}
            self.status = "Tema stilleri geçersiz; varsayılan görünüm kullanılıyor"
        if (
            self.account_service
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
        self.settings_dialog.save_button.text = self.text("save", "Kaydet")
        self.settings_dialog.cancel_button.text = self.text("close", "Kapat")
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
        self.renderer = MessageRenderer()
        self._selection_render_key = None
        self.message_selection = False
        self.selected_message = 0
        self.selected_message_ref = None
        self.message_ranges = []
        self._transcript_signature = None
        self._transcript_cache = ""
        self.formatted_transcript_source = ""
        self.formatted_transcript = ANSI("")
        self.resume_items = self.resume_filtered = []
        self.resume_loading = False
        self.sidebar_rows = []
        self.conversation_id = None
        self.title = "New conversation"
        self.messages = []
        self.current_project = {}
        self.temporary = False
        self.pending_attachments = []
        self.input.text = ""
        settings_open = self.settings_dialog.visible
        self.tab_kind = "settings" if settings_open else "chat"
        self.title = "Ayarlar" if settings_open else "New conversation"
        self.tabs = retained_tabs + [dict(self._new_tab_state(), tab_id=uuid.uuid4().hex)]
        self.active_tab = len(self.tabs) - 1
        self.session_state = {"authenticated": False, "saved_snapshot": False}
        self.status = "Oturum kapatıldı · F12 ile giriş yapın"
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

    def render_sidebar_help(self):
        return FormattedText(
            [
                (
                    "class:help",
                    f" ↑↓ gezin · Enter aç · Space ±\n {self.shortcut('ui.search')} ara · {self.shortcut('ui.toggle_sidebar')} gizle",
                )
            ]
        )

    def render_sidebar(self) -> FormattedText:
        self._build_sidebar_rows()
        if not self.sidebar_rows:
            message = (
                "  Sohbetler yükleniyor…\n" if self.sidebar_loading else "  Sohbet bulunamadı\n"
            )
            return FormattedText([("class:sidebar.muted", message)])
        rendered: list[tuple[str, str]] = []
        for index, row in enumerate(self.sidebar_rows):
            kind = row["kind"]
            selected = index == self.sidebar_index
            if kind in ("section", "project"):
                marker = "▸" if row["collapsed"] else "▾"
                style = "class:sidebar.selected" if selected else "class:sidebar.header"
                count = str(row.get("count", ""))
                title = fit_text(row["title"], self.sidebar_width() - len(count) - 6)
                rendered.append((style, f" {marker} {title}  {count}\n"))
            elif kind == "message":
                rendered.append(
                    (
                        "class:sidebar.muted",
                        f"   {fit_text(row['title'], self.sidebar_width() - 3)}\n",
                    )
                )
            else:
                marker = "›" if selected else " "
                indent = "   " if row.get("nested") else "  "
                title = fit_text(row["title"], self.sidebar_width() - len(indent) - 2)
                rendered.append(
                    (
                        "class:sidebar.selected" if selected else "class:sidebar.item",
                        f"{indent}{marker} {title}\n",
                    )
                )
        fragments = []
        for i, (style, text) in enumerate(rendered):
            row = self.sidebar_rows[i]
            if row["kind"] == "project":
                fragments.append(
                    (
                        style,
                        fit_text(text.rstrip(), self.sidebar_width() - 5),
                        on_click(lambda i=i: self.click_sidebar(i)),
                    )
                )
                fragments.append(
                    (
                        "class:button",
                        " ⓘ\n",
                        on_click(
                            lambda project_id=row["id"]: self.show_project_details(project_id)
                        ),
                    )
                )
            else:
                fragments.append((style, text, on_click(lambda i=i: self.click_sidebar(i))))
        return FormattedText(fragments)

    def click_sidebar(self, index):
        self.sidebar_index = index
        self.activate_sidebar_item()

    def click_palette(self, index):
        self.palette_index = index
        self.execute_palette_item()

    def _build_sidebar_rows(self) -> None:
        query = self.sidebar_search.text.strip().casefold()
        conversations = [
            item
            for item in self.sidebar_items
            if item.get("kind") != "workspace"
            and (
                self.provider_filter is None
                or item.get("provider_id", "chatgpt") in self.provider_filter
            )
        ]
        workspaces = [item for item in self.sidebar_items if item.get("kind") == "workspace"]
        projects: dict[str, list[dict[str, Any]]] = (
            {key: [] for key in self.project_items}
            if self.provider_filter is None or "chatgpt" in self.provider_filter
            else {}
        )
        unassigned: list[dict[str, Any]] = []
        for item in conversations:
            project = item.get("project")
            project_id = item.get("project_id")
            if isinstance(project, dict):
                project_id = project.get("id") or project_id
            if isinstance(project_id, str) and project_id:
                projects.setdefault(project_id, []).append(item)
            else:
                unassigned.append(item)

        def matches(item: dict[str, Any]) -> bool:
            title = str(item.get("title") or item.get("name") or "").casefold()
            return not query or query in title or query in str(item.get("id", "")).casefold()

        rows: list[dict[str, Any]] = []
        rows.append(
            {
                "kind": "section",
                "id": "projects",
                "title": "Projeler",
                "count": len(projects),
                "collapsed": "section:projects" in self.sidebar_collapsed,
            }
        )
        if "section:projects" not in self.sidebar_collapsed:
            for project_id, items in sorted(
                projects.items(),
                key=lambda pair: str(
                    self.project_items.get(pair[0], {}).get("name") or pair[0]
                ).casefold(),
            ):
                project_meta = items[0].get("project") if items else {}
                project_meta = project_meta if isinstance(project_meta, dict) else {}
                remote = self.project_items.get(project_id, {})
                project_name = str(
                    remote.get("name")
                    or (items[0].get("project_name") if items else None)
                    or project_meta.get("name")
                    or project_id
                )
                visible_items = [item for item in items if matches(item)]
                if query and query in project_name.casefold():
                    visible_items = items
                if query and query not in project_name.casefold() and not visible_items:
                    continue
                collapsed = f"project:{project_id}" in self.sidebar_collapsed
                rows.append(
                    {
                        "kind": "project",
                        "id": project_id,
                        "title": project_name,
                        "count": len(items),
                        "collapsed": collapsed,
                    }
                )
                if not collapsed:
                    if project_id in self.project_chats_loading:
                        rows.append({"kind": "message", "title": "Sohbetler yükleniyor…"})
                    if project_id in self.project_chat_errors:
                        rows.append(
                            {"kind": "message", "title": self.project_chat_errors[project_id]}
                        )
                    rows.extend(self._chat_row(item, nested=True) for item in visible_items)
                    if self.project_chat_cursors.get(project_id):
                        rows.append(
                            {
                                "kind": "project_more",
                                "id": project_id,
                                "title": "Daha fazla sohbet…",
                            }
                        )
            if not projects:
                rows.append(
                    {
                        "kind": "message",
                        "title": "Projeler yükleniyor…"
                        if self.projects_loading
                        else self.project_error or "Henüz proje yok",
                    }
                )
            elif self.project_error:
                rows.append({"kind": "message", "title": f"Hata: {self.project_error}"})
            if self.project_cursor:
                rows.append({"kind": "projects_more", "title": "Daha fazla proje…"})

        visible_chats = [item for item in unassigned if matches(item)]
        rows.append(
            {
                "kind": "section",
                "id": "chats",
                "title": "Sohbetler",
                "count": len(unassigned),
                "collapsed": "section:chats" in self.sidebar_collapsed,
            }
        )
        if "section:chats" not in self.sidebar_collapsed:
            if self.sidebar_error:
                rows.append(
                    {
                        "kind": "message",
                        "title": f"Hata: {self.sidebar_error[:80]}",
                    }
                )
            rows.extend(self._chat_row(item) for item in visible_chats)
            if self.history_cursor:
                rows.append(
                    {
                        "kind": "history_more",
                        "title": "Yükleniyor…"
                        if self.history_loading
                        else "Daha eski sohbetleri yükle…",
                    }
                )
            if not visible_chats and unassigned:
                rows.append({"kind": "message", "title": "Aramayla eşleşen sohbet yok"})

        layout_start = len(rows)
        visible_workspaces = [item for item in workspaces if matches(item)]
        rows.append(
            {
                "kind": "section",
                "id": "layouts",
                "title": "Düzenler",
                "count": len(workspaces),
                "collapsed": "section:layouts" in self.sidebar_collapsed,
            }
        )
        if "section:layouts" not in self.sidebar_collapsed:
            rows.extend(
                {
                    "kind": "workspace",
                    "id": item.get("id"),
                    "title": str(item.get("name") or "Adsız çalışma alanı"),
                }
                for item in visible_workspaces
            )
            if self.workspace_error:
                rows.append({"kind": "message", "title": "Düzen eklentisi kullanılamıyor"})
            elif not workspaces:
                rows.append({"kind": "message", "title": "Henüz düzen yok"})
        if not getattr(self, "layout_enabled", True):
            rows = rows[:layout_start]
        if self.account_service and not projects and self.provider_id == "gemini":
            rows = [
                row
                for row in rows
                if not (row["kind"] == "section" and row.get("id") == "projects")
            ]
        self.sidebar_rows = rows
        self.sidebar_index = min(self.sidebar_index, max(0, len(rows) - 1))

    @staticmethod
    def _chat_row(item: dict[str, Any], nested: bool = False) -> dict[str, Any]:
        return {
            "kind": "chat",
            "id": item.get("id"),
            "project_id": item.get("project_id"),
            "title": ({"chatgpt": "◈ ", "gemini": "✦ "}.get(item.get("provider_id"), ""))
            + str(item.get("title") or "Adsız sohbet"),
            "nested": nested,
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
        self.status = "Sohbetler"
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
        cursor = self.history_cursor

        def loaded(page):
            by_id = {item["id"]: item for item in self.sidebar_items if item.get("id")}
            for item in page["items"]:
                if item.get("id"):
                    by_id[item["id"]] = item
            self.sidebar_items = list(by_id.values())
            self.history_cursor = page.get("cursor") if page.get("cursor") != cursor else None
            self.history_loading = False
            self.filter_sidebar()

        def failed(reason):
            self.history_loading = False
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
                    partial_error = "Yenilenemeyen hesaplar: " + ", ".join(page["errors"])
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
                    by_id = {
                        item["id"]: item for item in project_chats + conversations if item.get("id")
                    }
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
                    self.status = partial_error or "Sol panel yenilenemedi; son kayıtlar korunuyor"
                self.filter_sidebar()
                if getattr(self, "sidebar_refresh_pending", False):
                    self.sidebar_refresh_pending = False
                    self.refresh_sidebar()

            self._dispatch_ui(apply_results)

        threading.Thread(target=load, daemon=True).start()

    def _read_in_background(self, read, success, failure, session_bound=True) -> None:
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

        threading.Thread(target=work, daemon=True).start()

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
                failure("Sağlayıcı model kataloğu geçersiz")
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
                selected = next((m for m in catalog.chat_models if m.id == tab.get("model")), None)
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
            self.session_state["authenticated"] = catalog.state == FeatureState.AVAILABLE
            tab = self.tabs[self.active_tab]
            self.model = tab.get("model") or ""
            self.effort = tab.get("effort")
            self.last_response_model = tab.get("response_model")
            if not self.active_busy():
                self.status = f"{len(catalog.chat_models)} model · hazır"
            if self.palette_visible and self.palette_mode in ("models", "effort"):
                query = self.palette_search.text
                self.open_palette(self.palette_mode)
                self.palette_search.text = query
            self.app.invalidate()

        def failure(reason):
            finish()
            if epoch == self.session_epoch and self.account_key == key:
                self.catalog_error = reason
                self.status = f"Model kataloğu alınamadı: {reason} · F10 yeniden dene"
            self.app.invalidate()

        self._read_in_background(callback, success, failure, session_bound=False)

    def normalize_model_selection(self):
        if self.catalog.state != FeatureState.AVAILABLE or self.active_busy():
            return
        selected = next((m for m in self.catalog.chat_models if m.id == self.model), None)
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
            self.status = "Yanıt tamamlanınca bu sohbetin modelini değiştirebilirsiniz"
            self.app.invalidate()
            return
        selected = next((m for m in self.catalog.chat_models if m.id == model_id), None)
        if selected is None:
            self.status = "Bu model normal sohbet kataloğunda yok · F10 yenile · /model ile seçin"
        else:
            self.model = selected.id
            self.effort = (
                self.catalog.effort(selected.id, self.effort_preference)
                if self.effort_preference
                else None
            )
            self.remember_selection()
            self._save_active_tab()
            self.status = f"Model: {selected.chat_title or selected.title} · efor: {self.effort or 'sağlayıcı varsayılanı'}"
        self.app.invalidate()

    def select_effort(self, effort: str | None) -> None:
        if self.tabs[self.active_tab].get("busy"):
            self.status = "Yanıt tamamlanınca bu sohbetin eforunu değiştirebilirsiniz"
            self.app.invalidate()
            return
        selected = next((m for m in self.catalog.chat_models if m.id == self.model), None)
        if effort is not None and (
            selected is None or effort not in {e.id for e in selected.efforts}
        ):
            self.status = "Bu efor seçili modelin kataloğunda yok · /effort ile seçenekleri görün"
        else:
            self.effort = effort
            self.effort_preference = effort or ""
            self.remember_selection()
            self._save_active_tab()
            self.status = f"Efor: {effort or 'sağlayıcı varsayılanı'}"
        self.app.invalidate()

    def refresh_projects(self, more: bool = False) -> None:
        if Capability.PROJECTS not in self.provider_features:
            self.projects_loading = False
            self.project_items = {}
            self.projects_loaded = True
            return
        account_key = self.account_key
        if self.list_projects_callback is None or account_key in self._project_requests:
            return
        cursor = self.project_cursor if more else None
        token = uuid.uuid4().hex
        self._project_requests[account_key] = token
        self.projects_loading = True
        callback = self.list_projects_callback
        epoch = self.session_epoch

        def finish():
            if self._project_requests.get(account_key) == token:
                self._project_requests.pop(account_key, None)
            self.projects_loading = self.account_key in self._project_requests

        def success(result):
            finish()
            if epoch != self.session_epoch or self.account_key != account_key:
                return
            if not isinstance(result, dict) or not isinstance(result.get("items"), list):
                failure("Proje listesi geçersiz")
                return
            next_cursor = result.get("cursor")
            if cursor and next_cursor == cursor:
                failure("Sağlayıcı aynı proje sayfasını döndürdü")
                return
            projects = dict(self.project_items) if more else {}
            for item in result["items"]:
                if not isinstance(item, dict) or not item.get("id"):
                    failure("Proje kimliği eksik")
                    return
                if item["id"] not in self.project_items:
                    self.sidebar_collapsed.add(f"project:{item['id']}")
                projects[item["id"]] = dict(item, name=item.get("name") or item["id"])
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
            self.project_error = None
            self.filter_sidebar()
            if self.palette_visible and self.palette_mode == "projects":
                self.open_project_picker()

        def failure(reason):
            finish()
            if epoch != self.session_epoch or self.account_key != account_key:
                return
            self.project_error = reason
            self.status = f"Proje listesi alınamadı: {reason}"
            self.filter_sidebar()

        self._read_in_background(lambda: callback(cursor), success, failure, session_bound=False)

    def load_project_chats(self, project_id: str, more: bool = False) -> None:
        if self.list_project_chats_callback is None or project_id in self.project_chats_loading:
            return
        cursor = self.project_chat_cursors.get(project_id) if more else None
        self.project_chats_loading.add(project_id)

        def success(result):
            if not isinstance(result, dict) or not isinstance(result.get("items"), list):
                failure("Proje sohbetleri geçersiz")
                return
            next_cursor = result.get("cursor")
            if cursor and next_cursor == cursor:
                failure("Sağlayıcı aynı sohbet sayfasını döndürdü")
                return
            items = {item["id"]: item for item in self.sidebar_items if item.get("id")}
            for item in result["items"]:
                if not isinstance(item, dict) or not item.get("id"):
                    failure("Proje sohbet kimliği eksik")
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
            self.status = "Sol panelde bir proje seçip F9'a basın"
            self.app.invalidate()
            return
        if self.get_project_callback is None:
            self.status = "Sağlayıcının proje ayrıntısı bağlantısı kurulmamış"
            self.app.invalidate()
            return
        if self.busy or self.loading_conversation or self.background_jobs or self.uploading_image:
            self.status = "Etkin işlem bitince proje ayrıntılarını açabilirsiniz"
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
        self.tab_kind = "project"
        self.current_project = dict(self.project_items.get(project_id, {"id": project_id}))
        self.title = self.current_project.get("name") or project_id
        self.messages = [{"role": "system", "text": "Proje bilgileri yükleniyor…"}]
        self._save_active_tab()
        self.scroll_bottom()

        def success(project):
            self.project_items[project_id] = project
            resources = project.get("resources") or []
            text = f"# {project.get('name') or project_id}\n\n{project.get('description') or ''}\n\n**Proje kimliği:** `{project_id}`\n\n## Talimatlar\n\n{project.get('instructions') or 'Talimat yok.'}\n\n## Kaynaklar\n\n"
            text += (
                "\n".join(
                    f"- {item.get('name') or item.get('file_id') or item.get('id')} ({item.get('type') or 'dosya'})"
                    for item in resources
                )
                or "Kaynak yok."
            )
            update(text, project)

        def update(text, project):
            for index, tab in enumerate(self.tabs):
                if tab.get("kind") == "project" and tab.get("project", {}).get("id") == project_id:
                    tab.update(
                        messages=[{"role": "system", "text": text}],
                        project=project,
                        title=project.get("name") or project_id,
                    )
                    if index == self.active_tab:
                        self._restore_active_tab(focus=False)
                    break
            self.filter_sidebar()

        project_snapshot = dict(self.current_project)
        self._read_in_background(
            lambda: self.get_project_callback(project_id),
            success,
            lambda reason: update(
                f"Proje bilgileri alınamadı: {reason}\n\nF9 ile yeniden deneyin.", project_snapshot
            ),
        )

    def _dispatch_ui(self, callback: Callable[[], None]) -> None:
        loop = self.event_loop or getattr(self.app, "loop", None)
        if loop is not None:
            if loop.is_running():
                loop.call_soon_threadsafe(lambda: callback() if not self.app.is_done else None)
            return
        callback()

    def filter_sidebar(self) -> None:
        if hasattr(self, "app"):
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
        self.sidebar_index = 0
        self.sidebar_search.text = ""
        self.refresh_sidebar()
        self.app.layout.focus(self.sidebar_control)
        self.status = "Projeler"
        self.app.invalidate()

    def dispatch_action(self, action_id):
        handler = self.action_handlers.get(action_id)
        if handler is None:
            raise ValueError("Bilinmeyen arayüz aksiyonu")
        handler(SimpleNamespace(app=self.app))

    def run_action(self, action_id: str, arguments: dict[str, Any] | None = None) -> None:
        if action_id in self.action_handlers:
            self.dispatch_action(action_id)
            return
        if self.invoke_action is None:
            self.status = f"Action {action_id} is unavailable"
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
                        raise ValueError("Zorunlu alanları doldurun")
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
        target_tab = self.active_tab
        target_messages = self.messages
        self.background_jobs += 1

        def invoke() -> None:
            preview = None
            error_text = None
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
                if preview is not None:
                    with self.lock:
                        target_messages.append(
                            {
                                "role": "system",
                                "text": f"**{action_id}**\n\n```text\n{preview}\n```",
                            }
                        )
                tab = self.tabs[target_tab]
                tab["messages"] = target_messages
                tab["completed_pending"] = target_tab != self.active_tab
                if target_tab == self.active_tab:
                    self.messages = target_messages
                    self.status = (
                        f"Action failed: {error_text}"
                        if error_text
                        else f"Action completed: {action_id}"
                    )
                    self.scroll_bottom()
                else:
                    self.status = f"Action completed in tab {target_tab + 1}"
                    self.app.invalidate()

            self._dispatch_ui(show_result)

        threading.Thread(target=invoke, daemon=True).start()

    def activate_sidebar_item(self) -> None:
        self._build_sidebar_rows()
        if not self.sidebar_rows:
            return
        item = self.sidebar_rows[self.sidebar_index]
        if item["kind"] in ("section", "project"):
            self.toggle_sidebar_group()
        elif item["kind"] == "chat":
            conversation_id = item.get("id")
            if isinstance(conversation_id, str) and conversation_id:
                if self.background_jobs or self.uploading_image:
                    self.status = "Finish the active operation before loading another chat"
                    self.app.invalidate()
                    return
                existing_tab = next(
                    (
                        index
                        for index, tab in enumerate(self.tabs)
                        if tab.get("conversation_id") == conversation_id
                    ),
                    None,
                )
                if self.busy:
                    if existing_tab is not None:
                        self.active_tab = existing_tab
                        self._restore_active_tab()
                    else:
                        self.status = "Yanıt bitince bu sohbeti açabilirsiniz; açık sekmeler arasında geçiş serbest"
                        self.app.invalidate()
                    return
                if existing_tab is not None:
                    self.active_tab = existing_tab
                    self._restore_active_tab()
                    if self.layout_controller:
                        self.layout_controller.attach()
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
            {"id": key, "title": action_title(key), "detail": self.shortcut(key)}
            for key in sorted(self.action_handlers, key=action_order)
        ]
        if mode != "actions":
            self.palette_items = []
        if mode != "effort":
            self.palette_items.extend(
                {
                    "id": f"model.select:{model.id}",
                    "title": f"Model · {model.chat_title or model.title}"
                    + (" ✓" if model.id == self.model else ""),
                    "detail": model.id,
                }
                for model in self.catalog.chat_models
            )
        selected = next((m for m in self.catalog.chat_models if m.id == self.model), None)
        if mode != "models" and selected:
            self.palette_items.extend(
                {
                    "id": f"effort.select:{choice.id}",
                    "title": f"Efor · {choice.title}" + (" ✓" if choice.id == self.effort else ""),
                    "detail": f"{choice.id} · {selected.chat_title or selected.title}",
                }
                for choice in selected.efforts
            )
            self.palette_items.append(
                {
                    "id": "effort.default",
                    "title": "Efor · sağlayıcı varsayılanı",
                    "detail": "Ek efor tercihi gönderme",
                }
            )
        try:
            if mode == "actions":
                self.palette_items.extend(self.list_actions())
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
            message = "Eşleşen komut yok"
            if self.palette_mode == "models":
                message = (
                    "Model kataloğu yükleniyor…"
                    if self.catalog_loading
                    else f"Model kataloğu boş veya alınamadı: {self.catalog_error or 'F10 ile yenileyin'}"
                )
            elif self.palette_mode == "effort":
                message = "Önce /model ile model seçin; efor seçenekleri seçili modele bağlıdır"
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
        if action_id.startswith("project.select:"):
            self.choose_project(action_id.partition(":")[2])
            return
        if action_id == "project.more":
            self.refresh_projects(more=True)
            self.open_project_picker()
            return
        if action_id == "command.temporary":
            self.new_temporary_chat()
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
            self.status = f"Action {action_id} cannot be invoked from this view"
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
            self.settings_dialog.category_control if self.tab_kind == "settings" else self.input
        )
        self.app.invalidate()

    def request_quit(self):
        dirty = bool(self.input.text) or any(tab.get("draft") for tab in self.tabs)
        if (dirty or self.busy) and time.monotonic() > getattr(self, "quit_armed_until", 0):
            self.quit_armed_until = time.monotonic() + 3
            self.status = (
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

    def _sync_activity(self) -> None:
        running = [i for i, tab in enumerate(self.tabs) if tab.get("busy")]
        self.busy = bool(running)
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
        if self.loading_conversation or self.uploading_image:
            self.status = "Wait for the current load or upload before creating a tab"
            self.app.invalidate()
            return
        self._save_active_tab()
        self.tabs.append(
            {
                "account_key": self.account_key,
                "catalog": self.catalog,
                "effort_preference": self.effort_preference,
                "conversation_id": None,
                "title": "New conversation",
                "messages": [],
                "model": self.model,
                "effort": self.effort,
                "project": {},
                "pending_attachments": [],
                "busy": False,
                "completed_pending": False,
            }
        )
        self.active_tab = len(self.tabs) - 1
        self._restore_active_tab()
        self.restore_account_preference()
        self._save_active_tab()

    def switch_tab(self, offset: int) -> None:
        self.close_overlays()
        if self.loading_conversation or self.uploading_image or len(self.tabs) < 2:
            if self.loading_conversation or self.uploading_image:
                self.status = "Wait for the current load or upload before switching tabs"
                self.app.invalidate()
            return
        self._save_active_tab()
        self.active_tab = (self.active_tab + offset) % len(self.tabs)
        self._restore_active_tab()

    def _restore_active_tab(self, *, focus: bool = True) -> None:
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
        self.pending_attachments = list(tab.get("pending_attachments") or [])
        self._sync_activity()
        completed_pending = bool(tab.get("completed_pending"))
        tab["completed_pending"] = False
        if self.set_active_conversation is not None:
            self.set_active_conversation(None if self.temporary else self.conversation_id)
        if tab.get("busy"):
            self.status = "Streaming response…"
        elif self.busy and self.busy_tab_index is not None:
            self.status = f"Yanıt {self.busy_tab_index + 1}. sekmede sürüyor"
        elif completed_pending:
            self.status = "Yanıt bu sekmede tamamlandı"
        else:
            self.status = f"Sekme {self.active_tab + 1}/{len(self.tabs)}"
        self.follow_output = tab.get("follow_output", True)
        self.transcript_window.vertical_scroll = (
            10**9 if self.follow_output else tab.get("scroll", 0)
        )
        self.app.invalidate()
        if focus:
            self.app.layout.focus(
                self.settings_dialog.category_control if self.tab_kind == "settings" else self.input
            )
        if self.account_service and self.catalog.state == FeatureState.UNKNOWN:
            self.refresh_provider_catalog()

    def close_tab(self) -> None:
        self.close_overlays()
        if self.busy or self.loading_conversation or self.uploading_image or self.background_jobs:
            self.status = "Wait for active work to finish before closing a tab"
            self.app.invalidate()
            return
        closing = self.tabs[self.active_tab]
        if closing.get("temporary") and closing.get("conversation_id") and self.forget_temporary:
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
                "title": "New conversation",
                "messages": [],
                "model": self.model,
                "effort": self.effort,
                "project": {},
                "pending_attachments": [],
                "busy": False,
                "completed_pending": False,
            }
            self.active_tab = 0
        else:
            self.tabs.pop(self.active_tab)
            self.active_tab = min(self.active_tab, len(self.tabs) - 1)
        self._restore_active_tab()

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
        if self.busy and self.busy_tab_index != self.active_tab:
            running = (self.busy_tab_index or 0) + 1
            return FormattedText(
                [("", f" ● Yanıt {running}. sekmede sürüyor · diğer sekmelere geçebilirsiniz ")]
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
                "Yanıt üretiliyor"
                if self.busy
                else "Sohbet yükleniyor"
                if self.loading_conversation
                else "Dosya yükleniyor"
            )
            return FormattedText([("", f" {frame} {phase} · {elapsed} · {self.status} ")])
        pending = [
            label
            for label, active in (
                ("Modeller", self.catalog_loading),
                ("Sohbetler", self.sidebar_loading),
                ("Projeler", self.projects_loading),
            )
            if active
        ]
        if pending:
            status = self.status if self.status not in ("Ready", "") else "Hazır"
            return FormattedText([("", f" • {status} · {', '.join(pending)} yenileniyor")])
        if self.status not in ("Ready", ""):
            return FormattedText([("", f" • {self.status} ")])
        if self.pending_attachments:
            names = ", ".join(item["name"] for item in self.pending_attachments)
            return FormattedText([("", f" 📎 {names} ")])
        if self.last_elapsed is not None:
            return FormattedText(
                [
                    ("", f" — Worked for {self.format_elapsed(self.last_elapsed)} "),
                ]
            )
        return FormattedText([("", " Hazır")])

    def render_transcript(self) -> str:
        width = self.transcript_width()
        with self.lock:
            signature = (
                width,
                self.busy,
                self.busy_tab_index,
                self.active_tab,
                self.show_reasoning,
                tuple((m.get("role"), m.get("text")) for m in self.messages),
            )
            if signature == getattr(self, "_transcript_signature", None):
                return self._transcript_cache
            messages = [dict(message) for message in self.messages]
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
                    "SiopeChat\n\nYeni bir sohbet başlatın.",
                    style="dim",
                )
            )
            rendered = buffer.getvalue()
        else:
            pieces = []
            self.message_ranges = []
            line = 0
            # Recaps from history belong before the final answer of that turn.
            ordered, pending = [], []
            for entry in enumerate(messages):
                if entry[1]["role"] in ("user", "system"):
                    ordered.extend(sorted(pending, key=lambda item: item[1]["role"] != "reasoning"))
                    pending = []
                    ordered.append(entry)
                else:
                    pending.append(entry)
            ordered.extend(sorted(pending, key=lambda item: item[1]["role"] != "reasoning"))
            for index, message in ordered:
                role = message["role"]
                if role == "reasoning" and not self.show_reasoning:
                    continue
                text = render_chatgpt_annotations(message["text"])
                active = (
                    self.busy
                    and self.busy_tab_index == self.active_tab
                    and index == len(messages) - 1
                    and role == "assistant"
                )
                value = self.renderer.render(role, text, width, index > 0, active)
                self.message_ranges.append((line, line + value.count("\n"), index))
                line += value.count("\n")
                pieces.append(value)
            rendered = "".join(pieces)
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
            self.status = "Layout eklentisi kullanılamıyor"
            self.app.invalidate()

    def transcript_width(self) -> int:
        return min(
            getattr(self, "theme_options", {}).get("message_width", 100),
            max(8, self.main_width() - 5),
        )

    def render_formatted_transcript(self):
        rendered = self.render_transcript()
        reference = getattr(self, "selected_message_ref", None)
        if reference is not None:
            self.selected_message = next(
                (i for i, item in enumerate(self.messages) if item is reference),
                self.selected_message,
            )
        key = (rendered, self.selected_message, self.message_selection)
        if key != getattr(self, "_selection_render_key", None):
            fragments, line, range_position = [], 0, 0
            for style, text in ANSI(rendered).__pt_formatted_text__():
                for part in text.splitlines(keepends=True):
                    while (
                        range_position < len(self.message_ranges)
                        and line >= self.message_ranges[range_position][1]
                    ):
                        range_position += 1
                    index = (
                        self.message_ranges[range_position][2]
                        if range_position < len(self.message_ranges)
                        else None
                    )
                    selected = self.message_selection and index == self.selected_message
                    fragments.append(
                        (
                            style + (" class:message.selected" if selected else ""),
                            part,
                            on_click(lambda index=index: self.select_message(index)),
                        )
                    )
                    line += part.count("\n")
            self.formatted_transcript = FormattedText(fragments)
            self.formatted_transcript_source = rendered
            self._selection_render_key = key
        return self.formatted_transcript

    def select_message(self, index):
        if index is None:
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
            return FormattedText([("class:resume.time", " Loading conversations…")])
        if not self.resume_filtered:
            return FormattedText([("class:resume.time", " No matching conversations")])
        fragments: list[tuple[str, str]] = []
        for index, item in enumerate(self.resume_filtered):
            title = item.get("title") or "Untitled"
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
        if force:
            self.follow_output = True
        if self.follow_output:
            self.transcript_window.vertical_scroll = 10**9
        self.app.invalidate()

    def submit_prompt(self) -> None:
        if (
            self.account_service
            and hasattr(self.account_service, "enabled")
            and not self.account_service.enabled(self.provider_id)
        ):
            self.status = "Sağlayıcı devre dışı · taslak korundu · Ayarlar → Sağlayıcılar"
            self.app.invalidate()
            return
        self.normalize_model_selection()
        if self.tab_kind == "project":
            self.status = "Bu sekme proje bilgilerini gösterir; sohbet listesinden bir sohbet açın"
            self.app.invalidate()
            return
        if not self.input.text.lstrip().startswith("/") and (
            self.model.casefold().endswith("-wm")
            or (
                self.catalog.state == FeatureState.AVAILABLE
                and not any(model.id == self.model for model in self.catalog.chat_models)
            )
        ):
            self.status = "Önce /model ile normal bir sohbet modeli seçin · taslak korundu"
            self.app.invalidate()
            return
        if self.loading_conversation or self.uploading_image:
            self.status = "Conversation or image still loading · draft kept"
            self.app.invalidate()
            return
        if self.background_jobs:
            self.status = "A background operation is active · draft kept"
            self.app.invalidate()
            return
        if self.active_busy() or sum(bool(tab.get("busy")) for tab in self.tabs) >= 4:
            self.status = "Bu sohbetin yanıtını bekleyin · aynı anda en fazla 4 bekleyen istek"
            self.app.invalidate()
            return
        prompt = self.input.text.strip()
        attachments = list(self.pending_attachments)
        if not prompt and not attachments:
            return
        self.input.text = ""
        if prompt.startswith("/"):
            self.handle_command(prompt)
            return
        self.pending_attachments.clear()
        prompt = prompt or "Describe the attached image."
        display_prompt = prompt
        if attachments:
            labels = " ".join(f"[📎 {item['name']}]" for item in attachments)
            display_prompt = f"{display_prompt}\n\n{labels}"
        with self.lock:
            self.messages.append({"role": "user", "text": display_prompt})
            self.messages.append({"role": "assistant", "text": ""})
        self._save_active_tab()
        self.busy = True
        self.busy_tab_index = self.active_tab
        self.tabs[self.active_tab]["busy"] = True
        self.tabs[self.active_tab]["completed_pending"] = False
        self.active_started = time.monotonic()
        self.tabs[self.active_tab]["started"] = self.active_started
        self.tabs[self.active_tab]["connection"] = None
        self.last_elapsed = None
        self.status = "Preparing secure request…"
        self.scroll_bottom()
        threading.Thread(
            target=self.run_send,
            kwargs={"send_callback": self.send_prompt},
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
        if command.strip() == "/help" and self.tab_kind == "settings":
            self.new_tab()
        if command.strip() in ("/login", "/logout", "/session"):
            self.session_dialog.open()
            return
        name, _, argument = command.partition(" ")
        if name in ("/exit", "/quit"):
            self.request_quit()
        elif name == "/new":
            self.new_tab()
        elif name == "/tab-next":
            self.switch_tab(1)
        elif name == "/tab-prev":
            self.switch_tab(-1)
        elif name == "/tab-close":
            self.close_tab()
        elif name == "/search":
            self.open_sidebar_search()
        elif name == "/project-new":
            self.open_project_create()
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
        elif name == "/refresh":
            self.refresh_sidebar()
            self.refresh_provider_catalog()
        elif name == "/action":
            action_id, _, raw_arguments = argument.strip().partition(" ")
            if action_id:
                try:
                    arguments = json.loads(raw_arguments) if raw_arguments else {}
                    if not isinstance(arguments, dict):
                        raise ValueError("action arguments must be a JSON object")
                    self.run_action(action_id, arguments)
                except (json.JSONDecodeError, ValueError) as error:
                    self.status = f"Usage: /action ID [JSON_OBJECT] · {error}"
                    self.app.invalidate()
            else:
                self.open_palette()
        elif name == "/clear":
            with self.lock:
                self.messages.clear()
            self.status = "Transcript cleared"
            self.scroll_bottom()
        elif name == "/resume":
            if argument.strip():
                self.resume_conversation(argument.strip())
            else:
                self.open_resume()
        elif name == "/history":
            if self.temporary:
                self.status = "Geçici sohbet yalnız açık oturumda tutulur"
                self.app.invalidate()
                return
            if self.conversation_id:
                self.resume_conversation(self.conversation_id)
            else:
                self.status = "No saved conversation yet"
                self.app.invalidate()
        elif name == "/rename":
            self.rename_current_conversation(argument)
        elif name == "/copy":
            self.copy_latest_response()
        elif name == "/remove":
            if self.pending_attachments:
                removed = self.pending_attachments.pop()
                self.status = f"Removed {removed['name']}"
            else:
                self.status = "No pending image to remove"
            self.app.invalidate()
        elif name == "/help":
            labels = {
                "ui.command_palette": "Komut ve eylem paletini aç",
                "ui.search": "Yüklenmiş sohbet listesini ara",
                "navigation.focus_sidebar": "Sol paneli göster ve odağı al",
                "ui.toggle_sidebar": "Sol paneli göster veya gizle",
                "chat.new": "Yeni sekme aç",
                "chat.close_tab": "Etkin sekmeyi kapat",
                "chat.next_tab": "Sonraki sekmeye geç",
                "chat.previous_tab": "Önceki sekmeye geç",
                "chat.resume": "Son sohbetleri ara ve aç",
                "chat.send": "Mesajı gönder",
                "chat.newline": "Mesaja yeni satır ekle",
                "chat.paste_image": "Metin veya görsel yapıştır",
                "edit.select_all": "Metnin veya odaktaki mesajın tümünü seç",
                "edit.cut": "Seçili metni kes",
                "chat.temporary": "Yeni geçici sohbet",
                "chat.interrupt": "Yanıtı durdur veya taslağı temizle",
                "chat.clear_transcript": "Görünen konuşmayı temizle",
                "chat.reload": "Etkin sohbeti yeniden yükle",
                "chat.copy_response": "Son yanıtı kopyala",
                "chat.remove_attachment": "Son eklenen görseli kaldır",
                "command.help": "Kısayol ve komut yardımını göster",
                "command.rename": "Etkin sohbeti yeniden adlandır",
                "command.projects": "Proje bölümünü göster",
                "command.project": "Seçili projenin bilgilerini aç",
                "command.refresh": "Sağlayıcı kataloglarını ve listeleri yenile",
                "command.settings": "Ayarları göster",
                "app.quit": "Uygulamadan çık",
                "app.quit_eof": "Boş girişte uygulamadan çık",
                "navigation.sidebar_up": "Sol panelde yukarı gezin",
                "navigation.sidebar_down": "Sol panelde aşağı gezin",
                "navigation.sidebar_toggle_group": "Seçili proje/bölümü daralt veya genişlet",
                "navigation.close_overlay": "Açık pencereyi kapat",
                "navigation.composer_next": "Komut tamamlamasını seç",
                "navigation.focus_composer": "Mesaj girişine odaklan",
                "navigation.transcript_up": "Konuşmada yukarı kaydır",
                "navigation.transcript_down": "Konuşmada aşağı kaydır",
                "navigation.transcript_start": "Konuşmanın başına git",
                "navigation.transcript_end": "Konuşmanın sonuna git",
            }
            shortcut_lines = "\n".join(
                f"- `{self.shortcut(action_id)}` — {labels.get(action_id, action_id)} (`{action_id}`)"
                for action_id, _key in sorted(self.keybindings.items())
            )
            navigation_help = (
                f"- `{self.shortcut('navigation.focus_sidebar')}`: Sol paneli aç ve odağı al\n"
                f"- `{self.shortcut('navigation.sidebar_up')}` / `{self.shortcut('navigation.sidebar_down')}`: proje ve sohbetlerde gezin; `Enter` sohbeti açar veya grubu değiştirir\n"
                f"- `{self.shortcut('navigation.sidebar_toggle_group')}`: seçili proje/bölümü daralt veya genişlet\n"
                f"- `{self.shortcut('ui.search')}`: yüklenmiş sohbetleri ara; `Enter` seçili sohbeti açar\n"
                f"- `{self.shortcut('ui.toggle_sidebar')}`: sol paneli gizle/göster\n"
                f"- `{self.shortcut('chat.resume')}`: son 50 sohbeti ara; `Enter` seçileni sekmede açar\n"
                f"- `{self.shortcut('chat.new')}` yeni sekme; `{self.shortcut('chat.previous_tab')}` / "
                f"`{self.shortcut('chat.next_tab')}` sekme değiştirir; `{self.shortcut('chat.close_tab')}` sekmeyi kapatır\n"
                "- Sekmede `●` yanıtın sürdüğünü, `✓` yanıtın tamamlandığını gösterir\n"
                "- Yanıt sürerken açık sekmelere geçilebilir; uyumluluk daemon'ı seri çalıştığı için başka sohbet yükleme/gönderme bekler\n"
            )
            self.append_system(
                "**Commands**\n\n"
                "- `/new` start a conversation\n"
                "- `/tab-next` or `/tab-prev` switch tabs\n"
                "- `/tab-close` close the current tab\n"
                "- `/search` search saved conversations\n"
                "- `/projects` list projects; `/proje` or `/project [ID]` move this chat or choose its project; `/project info` opens details\n"
                "- `/temporary` or `/private` starts a temporary chat without local conversation files\n"
                "- `/model [MODEL_ID]` choose from the account's model catalog\n"
                "- `/effort [ID|default]` choose an effort offered by the selected model\n"
                "- `/refresh` refresh models, projects and chats (F10 by default)\n"
                "- `/actions` open searchable actions and commands\n"
                "- `/settings` opens categorized settings and accounts in a workspace tab\n"
                "- `/action ID` run an application action\n"
                "- `/resume` search recent conversations\n"
                "- `/resume ID` open a conversation directly\n"
                "- `/history` reload this conversation\n"
                "- `/rename TITLE` rename this conversation\n"
                "- `/copy` copy the latest assistant response\n"
                "- `/remove` remove the latest pending image\n"
                "- `/clear` clear the displayed transcript\n"
                "- `/exit` or `/quit` quit"
                "\n\n**Text editing**\n\n"
                "Ctrl+A selects composer text; Ctrl+C copies a selection; Ctrl+V pastes. Click a message, then Ctrl+A / Ctrl+C to copy that message.\n"
                "Temporary chat uses the provider history-disabled request; provider retention rules still apply.\n"
                "\n\n**Navigation**\n\n"
                f"{navigation_help}"
                "\n**Configured shortcuts**\n\n"
                f"{shortcut_lines}"
            )
        else:
            self.status = f"Unknown command: {name}"
            self.app.invalidate()

    def rename_current_conversation(self, title: str) -> None:
        title = title.strip()
        if self.conversation_id is None:
            self.status = "Save the conversation before renaming it"
            self.app.invalidate()
            return
        if not title:
            self.status = "Usage: /rename TITLE"
            self.app.invalidate()
            return
        if self.rename_conversation is None:
            self.status = "Conversation rename is unavailable"
            self.app.invalidate()
            return
        conversation_id = self.conversation_id
        target_tab = self.active_tab
        self.status = "Renaming conversation…"
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
                    self.tabs[target_tab]["title"] = title
                if target_tab == self.active_tab:
                    if error_text is None:
                        self.title = title
                        self._save_active_tab()
                        self.status = "Conversation renamed"
                    else:
                        self.status = f"Rename failed: {error_text}"
                else:
                    self.status = (
                        f"Rename failed in tab {target_tab + 1}: {error_text}"
                        if error_text
                        else f"Rename completed in tab {target_tab + 1}"
                    )
                self.app.invalidate()

            self._dispatch_ui(apply_result)

        threading.Thread(target=rename, daemon=True).start()

    def paste_clipboard_image(self) -> None:
        if self.uploading_image:
            self.status = "An image upload is already in progress"
            self.app.invalidate()
            return
        if self.upload_image is None:
            self.status = "Image upload is unavailable"
            self.app.invalidate()
            return
        self.uploading_image = True
        self.background_jobs += 1
        self.upload_started = time.monotonic()
        self.status = "Reading image from clipboard…"
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
                attachment = self.upload_image(data, name, "image/png", width, height)
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
                        self.pending_attachments.append(attachment)
                        self.status = "Ready"
                    else:
                        self.status = f"Image paste failed: {message}"
                    self.app.invalidate()

                self._dispatch_ui(finish_upload)

        threading.Thread(target=upload, daemon=True).start()

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
            self.status = "No assistant response to copy"
            self.app.invalidate()
            return

        response = render_chatgpt_annotations(response)
        if clipboard.copy_text(response):
            self.status = "Copied latest response"
            self.app.invalidate()
            return
        self.app.output.write_raw(clipboard.terminal_sequence(response))
        self.app.output.flush()
        self.status = "Sent latest response to the terminal clipboard"
        self.app.invalidate()

    def open_resume(self) -> None:
        if self.palette_visible:
            self.close_overlays()
        if self.active_busy() or self.background_jobs or (self.busy and not self.account_service):
            self.status = "Bu sohbetin işlemi tamamlanınca başka bir sohbet yükleyin"
            self.app.invalidate()
            return
        if self.loading_conversation or self.resume_visible:
            return
        self.resume_visible = True
        self.resume_loading = True
        self.resume_items = []
        self.resume_filtered = []
        self.resume_index = 0
        self.resume_search.text = ""
        self.status = "Loading recent conversations…"
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
                    self.status = f"Conversation list failed: {message}"
                    self.app.invalidate()

                self._dispatch_ui(show_error)

        threading.Thread(target=load_items, daemon=True).start()

    def filter_resume(self) -> None:
        query = self.resume_search.text.strip().casefold()
        self.resume_filtered = [
            item
            for item in self.resume_items
            if query in str(item.get("title") or "Untitled").casefold()
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
        if self.temporary:
            count = len(self.tabs)
            self.new_tab()
            if len(self.tabs) == count:
                return
        if self.loading_conversation or self.uploading_image:
            self.status = "Yükleme tamamlanınca başka bir sohbet açabilirsiniz"
            self.app.invalidate()
            return
        if self.active_busy() or self.background_jobs or (self.busy and not self.account_service):
            self.status = "Bu sohbetin işlemi tamamlanınca başka bir sohbet yükleyin"
            self.app.invalidate()
            return
        if self.account_service:
            context, _ = origin(conversation_id, self.account_key)
            if context != self.account_key:
                self.bind_account(context)
                self.catalog = ModelCatalog(FeatureState.UNKNOWN)
                self.model, self.effort = "", None
                self.refresh_provider_catalog()
        target_tab = self.active_tab
        self.loading_conversation = True
        self.loading_started = time.monotonic()
        self.status = "Loading conversation…"
        self.app.layout.focus(self.input)
        self.app.invalidate()

        load_callback = self.load_conversation

        def load_selected() -> None:
            try:
                conversation = load_callback(conversation_id)
                messages = []
                for message in conversation.get("messages", []):
                    visible = visible_chat_message(message)
                    if visible is not None:
                        messages.append(visible)

                def apply_conversation() -> None:
                    if target_tab != self.active_tab:
                        return
                    with self.lock:
                        self.messages = messages
                    self.conversation_id = conversation_id
                    if self.set_active_conversation is not None:
                        self.set_active_conversation(conversation_id)
                    self.title = conversation.get("title") or "Untitled"
                    model = conversation.get("model") or conversation.get("default_model_slug")
                    if isinstance(model, str) and model:
                        self.model = model
                        if self.catalog.state == FeatureState.AVAILABLE:
                            selected = next(
                                (m for m in self.catalog.chat_models if m.id == model), None
                            )
                            if selected is None and self.catalog.default_chat_model is not None:
                                self.last_response_model = model
                                self.model = self.catalog.default_chat_model.id
                        self.effort = self.catalog.effort(self.model, self.effort_preference)
                    project = conversation.get("project")
                    project = dict(project) if isinstance(project, dict) else {}
                    project_id = conversation.get("project_id") or project.get("id")
                    project_name = conversation.get("project_name") or project.get("name")
                    if isinstance(project_id, str) and project_id:
                        project["id"] = project_id
                    if isinstance(project_name, str) and project_name:
                        project["name"] = project_name
                    project = dict(self.project_items.get(project_id, {})) | project
                    self.current_project = project
                    self.loading_conversation = False
                    self.loading_started = None
                    self.status = "Ready"
                    self._save_active_tab()
                    self.scroll_bottom()

                self._dispatch_ui(apply_conversation)
            except Exception as error:
                message = str(error)

                def show_error() -> None:
                    self.loading_conversation = False
                    self.loading_started = None
                    self.status = f"Load failed: {message}"
                    self.app.invalidate()

                self._dispatch_ui(show_error)

        threading.Thread(target=load_selected, daemon=True).start()

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
    ) -> None:
        rendered = ""
        stream_id = uuid.uuid4().hex
        started = self.tabs[tab_index].get("started", self.active_started)
        final_message_id = None
        elapsed = None
        final_status = "Ready"
        resolved_id = conversation_id
        resolved_title: str | None = None
        resolved_model: str | None = None

        def register_connection(connection: Any) -> None:
            self.tabs[tab_index]["connection"] = connection
            if tab_index == self.active_tab:
                self.cancel_connection = connection

        def on_event(event: dict[str, Any]) -> None:
            nonlocal rendered, final_message_id
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
                        item = {"role": "reasoning", "text": "", "stream_id": identity}
                        messages.insert(max(0, len(messages) - 1), item)
                    item["text"] = item["text"] + text if event_type == "delta" else text
                self._dispatch_ui(
                    lambda: (
                        self.scroll_bottom(force=False) if self.active_tab == tab_index else None
                    )
                )
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

            def update_active_view() -> None:
                if tab_index == self.active_tab:
                    self.status = "Streaming response…"
                    self.scroll_bottom(force=False)

            self._dispatch_ui(update_active_view)

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
                    messages[-1]["text"] = f"*Request stopped: {error}*"
            final_status = "Response stopped"
            if tab_index < len(self.tabs):
                self.tabs[tab_index]["pending_attachments"] = list(attachments)
        finally:
            if started is not None:
                elapsed = time.monotonic() - started

            def finish_turn() -> None:
                if tab_index >= len(self.tabs):
                    return
                tab = self.tabs[tab_index]
                tab["temporary"] = temporary
                tab["conversation_id"] = resolved_id
                tab["title"] = resolved_title or tab.get("title") or "New conversation"
                tab["messages"] = messages
                tab["model"] = model or resolved_model or ""
                tab["response_model"] = resolved_model
                tab["effort"] = effort
                tab["busy"] = False
                tab["completed_pending"] = tab_index != self.active_tab
                tab["started"] = None
                tab["connection"] = None
                self._sync_activity()
                if tab_index == self.active_tab:
                    self.conversation_id = resolved_id
                    self.title = tab["title"]
                    self.messages = messages
                    self.model = tab["model"]
                    self.last_response_model = resolved_model
                    self.effort = effort
                    self.pending_attachments = list(tab.get("pending_attachments") or [])
                    self.last_elapsed = elapsed
                    self.status = final_status
                    if self.set_active_conversation is not None:
                        self.set_active_conversation(None if tab.get("temporary") else resolved_id)
                    self._save_active_tab()
                    self.scroll_bottom(force=False)
                else:
                    self.status = f"Yanıt {tab_index + 1}. sekmede tamamlandı"
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

        try:
            from prompt_toolkit.patch_stdout import patch_stdout

            with patch_stdout(raw=True):
                self.app.run(pre_run=started)
        finally:
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
    ChatView(
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
    ).run()

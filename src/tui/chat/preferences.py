"""PreferencesComponent: preferences behavior for ChatView."""

from __future__ import annotations

import uuid

from providers.models import FeatureState, ModelCatalog
from settings import DEFAULT_KEYBINDINGS

from ..bindings import key_bindings
from ..markdown import (
    MessageRenderer,
)


class PreferencesComponent:
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
        render_labels = {
            "reasoning_label": self.tr("Düşünme özeti"),
            "waiting_label": self.tr("Yanıt hazırlanıyor…"),
        }
        if self.renderer.labels != render_labels:
            self.renderer = MessageRenderer(render_labels)
        self._transcript_signature = None
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
        self._project_chat_pages_loading.clear()
        self._project_chat_page_ids.clear()
        self._project_chat_refreshes.clear()
        self.project_chat_cursors.clear()
        self.project_chat_errors.clear()
        self.history_cursor = self.project_cursor = None
        self.history_loading = False
        render_labels = dict(self.renderer.labels)
        self.renderer = MessageRenderer(render_labels)
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

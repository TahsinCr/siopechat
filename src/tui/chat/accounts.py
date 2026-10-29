"""AccountComponent: accounts behavior for ChatView."""

from __future__ import annotations

from accounts import origin
from providers import Capability
from providers.models import FeatureState, ModelCatalog
from providers.registry import PROVIDERS

from ..library import SORT_OPTIONS
from ..styles import ICONS


class AccountComponent:
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
            self.status = (
                self.tr("Sağlayıcı devre dışı · Ayarlar")
                + f" {ICONS['right'].icon} "
                + self.tr("Sağlayıcılar")
            )
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
                    self._project_chat_page_ids = {
                        page: ids
                        for page, ids in self._project_chat_page_ids.items()
                        if page[0] != item
                    }
                    self._project_chat_pages_loading = {
                        page for page in self._project_chat_pages_loading if page[0] != item
                    }
                    self._project_chat_refreshes.pop(item, None)
                    for projects in self._account_projects.values():
                        projects.pop(item, None)
                self.remove_deleted_conversation_tabs(removed)
                # Project deletion can leave surviving chats open but unassigned.
                for tab in self.tabs:
                    if tab.get("project", {}).get("id") in removed:
                        tab.update(project={}, syncing=False)
                self._restore_active_tab(focus=False)
                self._persist_session()
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

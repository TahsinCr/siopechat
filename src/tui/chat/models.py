"""ModelComponent: models behavior for ChatView."""

from __future__ import annotations

import uuid
from typing import Any

from providers.models import FeatureState, ModelCatalog


class ModelComponent:
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
                self._update_model_catalog_status()
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

    def _selected_model_accounts(self) -> list[dict[str, Any]]:
        if not self.account_service or not hasattr(self.account_service, "active_accounts"):
            return [{"key": self.account_key}]
        try:
            accounts = list(self.account_service.active_accounts())
        except TypeError:
            return []
        return [item for item in accounts if isinstance(item, dict) and item.get("key")]

    def _model_catalog_status(self) -> str:
        accounts = self._selected_model_accounts()
        ready = 0
        models = 0
        for account in accounts:
            key = account["key"]
            catalog = self._account_catalogs.get(key)
            if (
                catalog is None
                and self.account_service
                and hasattr(self.account_service, "cached_models")
            ):
                catalog = self.account_service.cached_models(key)
                if catalog is not None:
                    self._account_catalogs[key] = catalog
            if catalog and catalog.state == FeatureState.AVAILABLE:
                ready += 1
                models += len(catalog.chat_models)
        return self.tr("{0} model · {1}/{2} sağlayıcı").format(models, ready, len(accounts))

    def _update_model_catalog_status(self) -> None:
        if (
            self.tab_kind == "chat"
            and not self.conversation_id
            and not self.messages
            and not self.active_busy()
        ):
            self.status = self._model_catalog_status()

    def _finish_model_catalog_request(self, key: str) -> None:
        self._model_picker_requests.discard(key)
        self._update_model_catalog_status()

    def refresh_other_provider_catalogs(self) -> None:
        """Load enabled providers' model catalogs without delaying chat startup."""
        if not self.account_service or not hasattr(self.account_service, "active_accounts"):
            return
        accounts = self._selected_model_accounts()
        self._update_model_catalog_status()
        for account in accounts:
            key = account["key"]
            catalog = self._account_catalogs.get(key)
            if catalog is None and hasattr(self.account_service, "cached_models"):
                catalog = self.account_service.cached_models(key)
                if catalog is not None:
                    self._account_catalogs[key] = catalog
            if key == self.account_key or key in self._model_picker_requests:
                continue
            self._model_picker_requests.add(key)

            def loaded(value, key=key):
                self._model_picker_requests.discard(key)
                self._account_catalogs[key] = value
                self._update_model_catalog_status()
                self.app.invalidate()

            def failed(_reason, key=key):
                self._finish_model_catalog_request(key)
                self.app.invalidate()

            self._read_in_background(
                lambda key=key: self.account_service.models(key),
                loaded,
                failed,
                session_bound=False,
            )

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

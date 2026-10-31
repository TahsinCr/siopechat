"""TabComponent: tabs behavior for ChatView."""

from __future__ import annotations

import shutil
import uuid
from typing import Any

from prompt_toolkit.layout.dimension import D

from accounts import origin
from providers.models import FeatureState, ModelCatalog


class TabComponent:
    def open_attachment(self):
        from ..attachments import AttachmentPicker

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

    def _persist_session(self) -> None:
        if self.session_store is None or self._restoring_session:
            return
        try:
            self._save_active_tab()
            layout = (
                self.layout_controller.snapshot("Temporary session")
                if self.layout_controller
                else None
            )
            self.session_store.save(self.tabs, self.active_tab, layout)
        except (OSError, TypeError, ValueError) as error:
            self.status = self.tr("Oturum düzeni kaydedilemedi: {0}").format(error)

    def restore_session(self, session: dict) -> bool:
        if not isinstance(session, dict) or not isinstance(session.get("tabs"), list):
            return False
        available_accounts = (
            {item["key"] for item in self.account_service.items()}
            if self.account_service
            else {self.account_key}
        )
        tabs = []
        source_indexes = []
        for index, saved in enumerate(session["tabs"]):
            if not isinstance(saved, dict):
                continue
            key = saved.get("account_key", "")
            if key and key not in available_accounts:
                continue
            project = saved.get("project")
            project = project if isinstance(project, dict) else {}
            conversation_id = saved.get("conversation_id")
            kind = saved.get("kind", "chat")
            tabs.append(
                {
                    "account_key": key or self.account_key,
                    "catalog": self._account_catalogs.get(key)
                    or ModelCatalog(FeatureState.UNKNOWN),
                    "conversation_id": conversation_id,
                    "kind": kind,
                    "title": saved.get("title") or self.tr("New conversation"),
                    "messages": [],
                    "model": saved.get("model", ""),
                    "effort": saved.get("effort"),
                    "effort_preference": saved.get("effort_preference") or "",
                    "draft": saved.get("draft", ""),
                    "project": dict(project),
                    "temporary": False,
                    "busy": False,
                    "loading": False,
                    "syncing": bool(conversation_id),
                    "completed_pending": False,
                    "follow_output": True,
                    "tab_id": saved["tab_id"],
                    "needs_hydration": bool(conversation_id) or kind == "project",
                    "restore_selection": bool(conversation_id),
                }
            )
            source_indexes.append(index)
        if not tabs:
            tabs = [dict(self._new_tab_state(), tab_id=uuid.uuid4().hex)]
            source_indexes = [0]
        active_source = session.get("active_tab", 0)
        self.active_tab = min(
            range(len(source_indexes)),
            key=lambda index: (
                abs(source_indexes[index] - active_source),
                source_indexes[index] > active_source,
            ),
        )
        self.tabs = tabs
        self.session_restored = True
        self._restoring_session = True
        try:
            self._restore_active_tab(focus=False)
            layout = session.get("layout")
            if self.layout_controller and isinstance(layout, dict):
                try:
                    self.layout_controller.restore(layout)
                    self._restore_active_tab(focus=False)
                except (KeyError, TypeError, ValueError):
                    pass
        finally:
            self._restoring_session = False
        return True

    def _hydrate_restored_tab(self, tab: dict[str, Any]) -> None:
        if not tab.get("needs_hydration"):
            return
        if self.loading_conversation and not self.account_service:
            return
        if tab.get("kind") == "project":
            project_id = (tab.get("project") or {}).get("id")
            if project_id:
                tab["needs_hydration"] = False
                model, effort = tab.get("model", ""), tab.get("effort")
                self.show_project_details(project_id)
                tab.update(model=model, effort=effort)
                if tab is self.tabs[self.active_tab]:
                    self.model, self.effort = model, effort
                    self._save_active_tab()
            return
        conversation_id = tab.get("conversation_id")
        if conversation_id:
            tab["needs_hydration"] = False
            tab["restore_selection"] = True
            self.resume_conversation(conversation_id)

    def new_tab(self) -> None:
        self.close_overlays()
        if self.loading_conversation and not self.account_service:
            self.status = self.tr("Wait for the current load before creating a tab")
            self.app.invalidate()
            return
        if self.tabs[self.active_tab].get("empty_workspace"):
            self.tabs[self.active_tab].update(
                conversation_id=None,
                title=self.tr("New conversation"),
                messages=[],
                project={},
                temporary=False,
                busy=False,
                completed_pending=False,
                loading=False,
                syncing=False,
                draft="",
                scroll=0,
                empty_workspace=False,
            )
            self._restore_active_tab()
            self.restore_account_preference()
            self._save_active_tab()
            self._persist_session()
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
        self._persist_session()

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
        if not self._restoring_session:
            self._hydrate_restored_tab(self.tabs[self.active_tab])
            self._persist_session()

    def _restore_active_tab(self, *, focus: bool = True) -> None:
        self.transcript_window.scroll_direction = 0
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
        elif tab.get("generation_complete"):
            self.status = self.tr("Response complete")
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
        self._remove_tab_at(self.active_tab)

    def _remove_tab_at(
        self, index, *, empty_workspace=False, save_active=True, restore=True, persist=True
    ):
        if not 0 <= index < len(self.tabs):
            return False
        if save_active:
            self._save_active_tab()
        old_active = self.active_tab
        closing = self.tabs[index]
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
        if closing.get("busy") and not empty_workspace:
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
        self.tabs.pop(index)
        if not self.tabs:
            self.tabs.append(
                {
                    "tab_id": uuid.uuid4().hex,
                    "account_key": self.account_key,
                    "catalog": self.catalog,
                    "effort_preference": self.effort_preference,
                    "conversation_id": None,
                    "title": "" if empty_workspace else self.tr("New conversation"),
                    "messages": [],
                    "model": self.model,
                    "effort": self.effort,
                    "project": {},
                    "busy": False,
                    "completed_pending": False,
                    "empty_workspace": empty_workspace,
                }
            )
            self.active_tab = 0
        elif index < old_active:
            self.active_tab = old_active - 1
        elif index == old_active:
            self.active_tab = max(0, index - 1)
        else:
            self.active_tab = old_active
        if self.layout_controller:
            self.layout_controller.reconcile_groups()
        if restore:
            self._restore_active_tab(focus=False)
        if persist:
            self._persist_session()
        return True

    def remove_deleted_conversation_tabs(self, conversation_ids):
        removed = set(conversation_ids)
        indexes = [
            index for index, tab in enumerate(self.tabs) if tab.get("conversation_id") in removed
        ]
        if not indexes:
            return

        self._save_active_tab()
        active = self.tabs[self.active_tab]
        active_id = active.get("tab_id")
        active_deleted = active.get("conversation_id") in removed
        previous_id = (
            self.tabs[self.active_tab - 1].get("tab_id")
            if self.active_tab > 0
            else self.tabs[1].get("tab_id")
            if len(self.tabs) > 1
            else None
        )
        for index in reversed(indexes):
            self._remove_tab_at(
                index,
                empty_workspace=True,
                save_active=False,
                restore=False,
                persist=False,
            )

        target_id = previous_id if active_deleted else active_id
        target_index = next(
            (i for i, tab in enumerate(self.tabs) if tab.get("tab_id") == target_id), None
        )
        if target_index is None:
            target_index = min(max(0, self.active_tab), len(self.tabs) - 1)
        self.active_tab = target_index
        self._restore_active_tab(focus=False)
        self._persist_session()

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
            self.layout_controller.reconcile_groups()
        self.status = self.tr("Sekmeler kapatıldı")
        self._persist_session()
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
        tab.pop("history_pending_edge", None)
        tab.pop("history_anchor", None)
        payload = tab.pop("render_payload", None)
        if payload is not None:
            payload.clear()
        cached_layout = tab.pop("render_layout", None)
        tab.pop("render_signature", None)
        if cached_layout is not None:
            cached_layout.dispose()
        self.transcript_control.clear_selection()
        archive = self.archive_service()
        if archive and tab.get("conversation_id"):
            key, remote = origin(tab["conversation_id"], tab.get("account_key", self.account_key))
            archive.release(key, remote)
        with self.lock:
            if tab.get("busy"):
                messages = tab.get("messages", [])
                start = next(
                    (
                        i
                        for i in range(len(messages) - 1, -1, -1)
                        if messages[i].get("role") == "user"
                    ),
                    max(0, len(messages) - 1),
                )
                del messages[:start]
                tab.pop("history_window", None)
                tab.pop("history_version", None)
            else:
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
            self._transcript_prefetch.clear()
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

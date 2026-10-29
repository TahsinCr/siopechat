"""ProjectComponent: projects behavior for ChatView."""

from __future__ import annotations

import uuid
from typing import Callable

from accounts import origin
from providers import Capability

from ..library import merge_chat_listing


class ProjectComponent:
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
        if not more:
            self._project_seen_cursors.clear()
        cursor = self.project_cursor if more else None
        if cursor:
            if cursor in self._project_seen_cursors:
                self.project_cursor = None
                return
            self._project_seen_cursors.add(cursor)
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
            if next_cursor and next_cursor in self._project_seen_cursors:
                next_cursor = None
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
            if next_cursor:
                self.refresh_projects(more=True)

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
        if self.list_project_chats_callback is None:
            return
        seen = self._project_chat_seen_cursors.setdefault(project_id, set())
        if not more:
            seen.clear()
            self.project_chat_errors.pop(project_id, None)
        cursor = self.project_chat_cursors.get(project_id) if more else None
        if cursor:
            if cursor in seen:
                self.project_chat_cursors[project_id] = None
                return
            seen.add(cursor)
        page_key = (project_id, cursor or "")
        if page_key in self._project_chat_pages_loading:
            return
        self._project_chat_pages_loading.add(page_key)
        self.project_chats_loading.add(project_id)
        self._invalidate_sidebar_rows()
        self.app.invalidate()

        def release_page() -> None:
            self._project_chat_pages_loading.discard(page_key)
            if not any(page[0] == project_id for page in self._project_chat_pages_loading):
                self.project_chats_loading.discard(project_id)

        def refresh_failed(reason):
            self._finish_project_chat_refresh(project_id)
            if project_id in self.project_items:
                self.project_chat_errors[project_id] = str(reason)
                self.filter_sidebar()

        def apply_page(result, *, cached=False, refreshing=False):
            if not isinstance(result, dict) or not isinstance(result.get("items"), list):
                reason = self.tr("Proje sohbetleri geçersiz")
                if refreshing:
                    refresh_failed(reason)
                else:
                    failure(reason)
                return
            release_page()
            if project_id not in self.project_items:
                if refreshing:
                    self._finish_project_chat_refresh(project_id)
                return
            next_cursor = result.get("cursor")
            if next_cursor and next_cursor in seen:
                next_cursor = None
            items = {item["id"]: item for item in self.sidebar_items if item.get("id")}
            page_ids = {
                item["id"] for item in result["items"] if isinstance(item, dict) and item.get("id")
            }
            for removed in self._project_chat_page_ids.get(page_key, set()) - page_ids:
                existing = items.get(removed)
                if existing and existing.get("project_id") == project_id:
                    items.pop(removed, None)
            for item in result["items"]:
                if not isinstance(item, dict) or not item.get("id"):
                    reason = self.tr("Proje sohbet kimliği eksik")
                    if refreshing:
                        refresh_failed(reason)
                    else:
                        failure(reason)
                    return
                normalized = dict(
                    item,
                    project_id=project_id,
                    project_name=self.project_items.get(project_id, {}).get("name"),
                )
                items[item["id"]] = merge_chat_listing(items.get(item["id"]), normalized)
            self.sidebar_items = list(items.values())
            self._project_chat_page_ids[page_key] = page_ids
            self.project_chats_loaded.add(project_id)
            self.project_chat_errors.pop(project_id, None)
            self.project_chat_cursors[project_id] = next_cursor
            self.filter_sidebar()
            if refreshing:
                self._finish_project_chat_refresh(project_id)
            if cached:
                self._start_project_chat_refresh(project_id)
                self._read_in_background(
                    lambda: self.list_project_chats_callback(project_id, cursor),
                    lambda refreshed: apply_page(refreshed, refreshing=True),
                    refresh_failed,
                    session_bound=False,
                )
            if next_cursor:
                self.load_project_chats(project_id, more=True)

        def failure(reason):
            release_page()
            if project_id in self.project_items:
                self.project_chat_errors[project_id] = str(reason)
            self.filter_sidebar()

        def read_page():
            cached_page = (
                self.account_service.cached_project_chats(project_id, self.account_key, cursor)
                if self.account_service
                else None
            )
            if isinstance(cached_page, dict) and isinstance(cached_page.get("items"), list):
                return cached_page, True
            return self.list_project_chats_callback(project_id, cursor), False

        self._read_in_background(
            read_page,
            lambda result: apply_page(result[0], cached=result[1]),
            failure,
            # Canonical project IDs are account-scoped; let their page finish
            # reconciling even if the user switches the active account meanwhile.
            session_bound=False,
        )

    def _start_project_chat_refresh(self, project_id: str) -> None:
        self._project_chat_refreshes[project_id] = (
            self._project_chat_refreshes.get(project_id, 0) + 1
        )
        self._invalidate_sidebar_rows()
        self.app.invalidate()

    def _finish_project_chat_refresh(self, project_id: str) -> None:
        pending = self._project_chat_refreshes.get(project_id, 0)
        if pending > 0:
            if pending > 1:
                self._project_chat_refreshes[project_id] = pending - 1
            else:
                self._project_chat_refreshes.pop(project_id, None)
        else:
            self._project_chat_refreshes.pop(project_id, None)
        self._invalidate_sidebar_rows()
        self.app.invalidate()

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
        self._persist_session()
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

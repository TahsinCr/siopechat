"""CommandComponent: commands behavior for ChatView."""

from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace
from typing import Any

from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.utils import get_cwidth

from accounts import origin
from providers import Capability
from providers.models import FeatureState, ModelCatalog

from ..actions import action_order, action_title
from ..icons import Icon
from ..widgets import (
    fit_text,
    on_click,
)


class CommandComponent:
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
            {"id": key, "title": action_title(key, self.translations), "detail": self.shortcut(key)}
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
                            self._update_model_catalog_status()
                            if self.palette_visible and self.palette_mode in ("models", "actions"):
                                query = self.palette_search.text
                                self.open_palette(self.palette_mode)
                                self.palette_search.text = query

                        def failed(_reason, key=key):
                            self._finish_model_catalog_request(key)

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
                            + (
                                f" {Icon.COMPLETE}"
                                if key == self.account_key and model.id == self.model
                                else ""
                            ),
                            "detail": key.split("/")[0] + " · " + model.id,
                        }
                    )
        selected = self.catalog.chat_model(self.model)
        if mode != "models" and selected:
            self.palette_items.extend(
                {
                    "id": f"effort.select:{choice.id}",
                    "title": self.tr("Efor · {0}").format(choice.title)
                    + (f" {Icon.COMPLETE}" if choice.id == self.effort else ""),
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
                    dict(
                        item,
                        title=action_title(item["id"], self.translations, item.get("title")),
                    )
                    if f"application.action_titles.{item['id']}" in self.translations
                    else dict(item, title=self.tr(item["title"]))
                    for item in self.list_actions()
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

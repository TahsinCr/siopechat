"""HistoryComponent: history behavior for ChatView."""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime

from prompt_toolkit.formatted_text import FormattedText

from accounts import origin
from providers.history import merge_conversation

from ..icons import Icon
from ..markdown import (
    visible_chat_messages,
)
from ..widgets import (
    fit_text,
    on_click,
)


class HistoryComponent:
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
            marker = Icon.FORWARD if index == self.resume_index else " "
            style = "class:resume.selected" if index == self.resume_index else "class:resume.item"
            width = max(8, self.terminal_width() - 10)
            value = fit_text(f" {marker} {title}  {stamp}", width)
            fragments.append((style, value + "\n", on_click(lambda i=index: self.click_resume(i))))
        return FormattedText(fragments)

    def click_resume(self, index):
        self.resume_index = index
        self.select_resume()

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
        if self.loading_conversation and not self.account_service:
            self.status = self.tr("Yükleme tamamlanınca başka bir sohbet açabilirsiniz")
            self.app.invalidate()
            return
        if self.active_busy():
            # Hydrate another chat in a fresh state; the streaming state stays intact.
            self.new_tab()
            if self.active_busy():
                return
        target_tab = self.tabs[self.active_tab]
        for key in (
            "archive_version",
            "archive_refresh_pending",
            "history_window",
            "history_version",
            "history_read",
            "history_pending_edge",
            "history_anchor",
        ):
            target_tab.pop(key, None)
        if self.account_service:
            key, remote = origin(conversation_id, self.account_key)
            if key != self.account_key:
                self.bind_account(key)
            restore_selection = bool(target_tab.pop("restore_selection", False))
            saved_model = target_tab.get("model") if restore_selection else None
            saved_effort = target_tab.get("effort") if restore_selection else None
            self.model, self.effort = "", None
            self.restore_account_preference()
            if saved_model:
                self.model = saved_model
                self.effort = saved_effort
            target_tab.update(
                account_key=key,
                catalog=self.catalog,
                model=self.model,
                effort=self.effort,
                effort_preference=self.effort_preference,
                temporary=False,
                kind="chat",
            )
        load_callback = self.load_conversation
        cached_layout = target_tab.pop("render_layout", None)
        target_tab.pop("render_signature", None)
        if cached_layout is not None:
            cached_layout.dispose()
        if getattr(self, "_virtual_tab_id", None) == target_tab.get("tab_id"):
            self._virtual_transcript = None
            self._virtual_signature = None
            self._virtual_pending_signature = None
            self._transcript_prefetch.clear()
        token = uuid.uuid4().hex
        self.loading_conversation = True
        self.loading_started = time.monotonic()
        self.status = self.tr("Loading conversation…")
        target_tab.update(
            loading=True,
            loading_started=self.loading_started,
            load_token=token,
            empty_workspace=False,
        )
        if self.account_service:
            # The conversation ID is already known, so keep the composer usable
            # while local history and provider synchronization arrive. Their
            # results are merged with any turn the user starts in the meantime.
            row = next((x for x in self.sidebar_items if x.get("id") == conversation_id), {})
            project_id = row.get("project_id")
            project = dict(self.project_items.get(project_id, {}))
            if project_id:
                project["id"] = project_id
            target_tab.update(
                conversation_id=conversation_id,
                messages=[],
                title=row.get("title") or self.tr("Sohbet"),
                project=project,
                loading=False,
                syncing=True,
                history_load_pending=True,
                history_load_complete=False,
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
                messages = visible_chat_messages(conversation.get("messages", []))

                def apply_conversation() -> None:
                    if (
                        not any(tab is target_tab for tab in self.tabs)
                        or target_tab.get("load_token") != token
                    ):
                        return
                    if target_tab is self.tabs[self.active_tab]:
                        self._save_active_tab()
                    visible_messages = messages
                    # History can finish after a local send has started. Merge
                    # against the current tab snapshot so pending turns and the
                    # active stream survive while missing history is inserted.
                    visible_messages = visible_chat_messages(
                        merge_conversation(
                            {"messages": visible_messages},
                            {"messages": target_tab.get("messages", [])},
                        )["messages"]
                    )
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
                        history_version=conversation.get("history_version"),
                        conversation_id=conversation_id,
                        title=conversation.get("title") or self.tr("Untitled"),
                        project=project,
                        loading=False,
                        loading_started=None,
                        load_error=None,
                        syncing=cached is not None and self.archive_service() is not None,
                        history_load_pending=cached is not None
                        and self.archive_service() is not None,
                        history_load_complete=False,
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
                        if previous is not None and not any(
                            item.get("render_layout") is previous for item in self.tabs
                        ):
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

                    def sync_failed(reason):
                        if (
                            not any(tab is target_tab for tab in self.tabs)
                            or target_tab.get("load_token") != token
                        ):
                            return
                        target_tab["syncing"] = False
                        target_tab.pop("history_load_pending", None)
                        target_tab.pop("history_load_complete", None)
                        if not target_tab.get("messages"):
                            target_tab["load_error"] = reason
                        self.status = self.tr("Senkronizasyon: ") + reason
                        self.app.invalidate()

                    def sync_completed(_result):
                        if (
                            not any(tab is target_tab for tab in self.tabs)
                            or target_tab.get("load_token") != token
                        ):
                            return
                        target_tab["history_load_complete"] = True
                        self.apply_archive_updates()

                    self._read_in_background(
                        lambda: self.account_service.load(
                            conversation_id, key, force=True, window=True
                        ),
                        sync_completed,
                        sync_failed,
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
                    target_tab.pop("history_load_pending", None)
                    target_tab.pop("history_load_complete", None)
                    if target_tab is self.tabs[self.active_tab]:
                        self.loading_conversation = False
                        self.loading_started = None
                        self.status = self.tr("Load failed: {0}").format(message)
                    self.app.invalidate()

                self._dispatch_ui(show_error)

        def enqueue_load(delay=0.1):
            if target_tab.get("load_token") != token or not any(
                tab is target_tab for tab in self.tabs
            ):
                return
            if self._reads.submit(load_selected):
                target_tab.pop("_load_retry_scheduled", None)
                return

            target_tab.update(loading=False, loading_started=None, syncing=True)
            target_tab["history_load_pending"] = True
            if target_tab is self.tabs[self.active_tab]:
                self.loading_conversation, self.loading_started = False, None
                self.status = self.tr("Sohbet geçmişi arka plan kuyruğunda bekliyor")
            if target_tab.get("_load_retry_scheduled"):
                self.app.invalidate()
                return
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                target_tab["needs_hydration"] = True
                self.app.invalidate()
                return

            target_tab["_load_retry_scheduled"] = True

            def retry():
                target_tab.pop("_load_retry_scheduled", None)
                if self.app.is_running:
                    enqueue_load(min(delay * 2, 1.0))

            loop.call_later(delay, retry)
            self.app.invalidate()

        enqueue_load()

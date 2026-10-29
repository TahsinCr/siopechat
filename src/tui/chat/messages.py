"""MessageComponent: messages behavior for ChatView."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from datetime import datetime

from accounts import origin
from utils import clipboard

from ..markdown import (
    message_body_text,
    render_chatgpt_annotations,
)


class MessageComponent:
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
        self._rename_conversation_record(conversation_id, title)

    def open_conversation_rename(self, conversation_id: str, current_title: str) -> None:
        if not conversation_id:
            self.status = self.tr("Save the conversation before renaming it")
            self.app.invalidate()
            return
        if self.rename_conversation is None:
            self.status = self.tr("Conversation rename is unavailable")
            self.app.invalidate()
            return

        def submit(values):
            title = str(values["title"]).strip()
            if not title:
                raise ValueError(self.tr("Yeni ad boş olamaz"))
            if title == current_title.strip():
                return
            self._rename_conversation_record(conversation_id, title)

        self.forms.open(
            self.tr("Sohbeti yeniden adlandır"),
            [("title", self.tr("Yeni ad"), current_title, None)],
            submit,
        )

    def _rename_conversation_record(self, conversation_id: str, title: str) -> None:
        if self.rename_conversation is None:
            self.status = self.tr("Conversation rename is unavailable")
            self.app.invalidate()
            return
        self.status = self.tr("Renaming conversation…")
        rename_callback = self.rename_conversation
        self.background_jobs += 1
        self.app.invalidate()

        def rename() -> None:
            error_text = None
            try:
                rename_callback(conversation_id, title)
            except Exception as error:
                error_text = str(error)

            def apply_result() -> None:
                self.background_jobs = max(0, self.background_jobs - 1)
                if error_text is None:
                    for item in self.sidebar_items:
                        if item.get("id") == conversation_id:
                            item["title"] = title
                    for tab in self.tabs + self.background_tabs:
                        if tab.get("conversation_id") == conversation_id:
                            tab["title"] = title
                    if self.conversation_id == conversation_id:
                        self.title = title
                        self._save_active_tab()
                    self._persist_session()
                    self._invalidate_sidebar_rows()
                    self.status = self.tr("Conversation renamed")
                else:
                    self.status = self.tr("Rename failed: {0}").format(error_text)
                self.app.invalidate()

            self._dispatch_ui(apply_result)

        if not self._operations.submit(rename):
            self.background_jobs = max(0, self.background_jobs - 1)
            self.status = self.tr("Arka plan iş kuyruğu dolu; yeniden deneyin")
            self.app.invalidate()

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
            self.show_temporary_status(self.tr("Copied latest response"))
            return
        self.app.output.write_raw(clipboard.terminal_sequence(response))
        self.app.output.flush()
        self.show_temporary_status(self.tr("Sent latest response to the terminal clipboard"))

    def show_temporary_status(self, value, duration=2.0):
        """Show brief feedback without overwriting a newer status message."""
        previous_is_temporary = self.status == self._temporary_status_text
        restore = self._temporary_status_restore if previous_is_temporary else self.status
        if self._temporary_status_handle is not None:
            self._temporary_status_handle.cancel()
        self._temporary_status_restore = restore
        self._temporary_status_text = value
        self.status = value

        def restore_status():
            if self.status == self._temporary_status_text:
                restored = self._temporary_status_restore
                self.status = restored if restored is not None else "Ready"
            self._temporary_status_handle = None
            self._temporary_status_text = None
            self._temporary_status_restore = None
            self.app.invalidate()

        self._temporary_status_handle = asyncio.get_running_loop().call_later(
            duration, restore_status
        )
        self.app.invalidate()

    def _toolbar_message_index(self, tab_id, message_ref):
        if not self.tabs or self.tabs[self.active_tab].get("tab_id") != tab_id:
            return None
        messages = self.tabs[self.active_tab].get("messages", ())
        index = next((i for i, item in enumerate(messages) if item is message_ref), None)
        if index is None:
            message_id = message_ref.get("id")
            ordinal = message_ref.get("_archive_ordinal")
            index = next(
                (
                    i
                    for i, item in enumerate(messages)
                    if message_id
                    and item.get("id") == message_id
                    and (ordinal is None or item.get("_archive_ordinal") == ordinal)
                ),
                None,
            )
        return index

    def message_toolbar_action(self, tab_id, message_ref, action):
        """Resolve the clicked tab/message before any clipboard, read or write action."""
        index = self._toolbar_message_index(tab_id, message_ref)
        if index is None:
            self.status = self.tr("This message is no longer loaded")
            self.app.invalidate()
            return
        messages = self.tabs[self.active_tab].get("messages", ())
        message = messages[index]
        if action == "sources":
            self._show_message_sources(message)
            return
        if action in ("version_prev", "version_next"):
            self._navigate_response_version(
                tab_id, message_ref, -1 if action == "version_prev" else 1
            )
            return
        if action in ("edit", "retry", "branch"):
            tab = self.tabs[self.active_tab]
            if (
                action not in message.get("actions", ())
                or not self.account_service
                or not tab.get("conversation_id")
                or tab.get("temporary")
                or tab.get("message_action_pending")
                or self.active_busy()
            ):
                return
            if any(
                other.get("busy") and other.get("conversation_id") == tab["conversation_id"]
                for other in self.tabs + self.background_tabs
            ):
                self.status = self.tr("Wait for this conversation to finish")
                self.app.invalidate()
                return
            if action == "branch":
                self._branch_message(tab, message)
            elif action == "edit":
                self.forms.open(
                    self.tr("Edit message"),
                    [("text", self.tr("Message"), message["text"], None)],
                    lambda values: (
                        lambda: self._submit_message_action(tab, message, "edit", values["text"])
                    ),
                )
            else:
                self._submit_message_action(tab, message, "retry", "")
            return
        if action == "copy":
            text = render_chatgpt_annotations(message_body_text(message))
            if not text:
                self.status = self.tr("This message has no text to copy")
            elif clipboard.copy_text(text):
                self.show_temporary_status(self.tr("Message copied"))
            else:
                self.app.output.write_raw(clipboard.terminal_sequence(text))
                self.app.output.flush()
                self.show_temporary_status(self.tr("Sent message to the terminal clipboard"))
            self.app.invalidate()
            return

    def _show_message_sources(self, message):
        from providers.messages import sources_from

        prepared_sources = message.get("_thinking_sources") or ()
        sources = sources_from({"sources": prepared_sources}) if prepared_sources else ()
        if not sources:
            sources = sources_from(message)
        if not sources:
            self.status = self.tr("No sources available")
            self.app.invalidate()
            return
        items = []
        for source in sources:
            title = " ".join(str(source.get("title") or "Source").split())[:200]
            url = source.get("url")
            description = source.get("description") or source.get("snippet") or ""

            def open_source(source_url=url):
                if not isinstance(source_url, str) or not source_url:
                    self.status = self.tr("Source link is unavailable")
                    self.app.invalidate()
                    return
                from ..markdown import open_link

                open_link(source_url)
                self.status = self.tr("Bağlantı tarayıcıda açıldı")
                self.app.invalidate()

            items.append(
                {
                    "url": url if isinstance(url, str) else "",
                    "title": title,
                    "description": description,
                    "callback": open_source if isinstance(url, str) else None,
                }
            )

        self.forms.open_link_list(self.tr("Used sources"), items)

    def _navigate_response_version(self, tab_id, message_ref, direction):
        index = self._toolbar_message_index(tab_id, message_ref)
        if index is None:
            return
        tab = self.tabs[self.active_tab]
        message = tab["messages"][index]
        message_id = message.get("id")
        current = message.get("_response_version_index", 1)
        count = message.get("_response_version_count", 1)
        if tab.get("response_version_loading") == message_id:
            requested = tab.get("response_version_target", current) + direction
            if 1 <= requested <= count:
                tab["response_version_target"] = requested
            return
        target = current + direction
        if (
            not message_id
            or not tab.get("conversation_id")
            or not tab.get("account_key")
            or not self.account_service
            or not 1 <= target <= count
        ):
            return
        tab["response_version_loading"] = message_id
        tab["response_version_target"] = target
        tab["follow_output"] = True
        self.follow_output = True
        self.transcript_window.scroll_direction = 0
        self.transcript_window.vertical_scroll = 10**9
        self.app.invalidate()
        conversation_id = tab["conversation_id"]
        account_key = tab["account_key"]

        def load_version(requested):
            def work():
                try:
                    result = self.account_service.response_version(
                        account_key, conversation_id, message_id, requested
                    )
                    reason = None if result else self.tr("Response version is unavailable")
                except Exception as error:
                    result, reason = None, str(error)

                def ready():
                    if tab.get("response_version_loading") != message_id:
                        return
                    latest = tab.get("response_version_target", requested)
                    if latest != requested:
                        load_version(latest)
                        return
                    tab.pop("response_version_loading", None)
                    tab.pop("response_version_target", None)
                    error_reason = reason
                    target_index = next(
                        (
                            position
                            for position, item in enumerate(tab.get("messages", ()))
                            if item.get("id") == message_id
                        ),
                        None,
                    )
                    if target_index is None:
                        return
                    if error_reason is None:
                        from ..markdown import visible_chat_message

                        alternate = visible_chat_message(result["message"])
                        if alternate is None:
                            error_reason = self.tr("Response version is unavailable")
                        else:
                            displayed = dict(tab["messages"][target_index])
                            displayed["text"] = alternate["text"]
                            for field in (
                                "_thinking_sources",
                                "_response_settings",
                                "_body_length",
                                "_files",
                            ):
                                if field in alternate:
                                    displayed[field] = alternate[field]
                                else:
                                    displayed.pop(field, None)
                            displayed["_response_version_index"] = result["index"]
                            displayed["_response_version_count"] = result["count"]
                            displayed["_response_version_current_index"] = result.get(
                                "current_index", result["count"]
                            )
                            tab["messages"][target_index] = displayed
                            if tab is self.tabs[self.active_tab]:
                                self.messages = tab["messages"]
                                tab["follow_output"] = True
                                self.follow_output = True
                                self.transcript_window.scroll_direction = 0
                                self.transcript_window.vertical_scroll = 10**9
                                tab["render_revision"] = tab.get("render_revision", 0) + 1
                    if error_reason:
                        self.status = self.tr("Response version could not be loaded: {0}").format(
                            error_reason
                        )
                    self.app.invalidate()

                self._dispatch_ui(ready)

            if not self._operations.submit(work):
                tab.pop("response_version_loading", None)
                tab.pop("response_version_target", None)
                self.status = self.tr("Arka plan iş kuyruğu dolu; yeniden deneyin")
                self.app.invalidate()

        load_version(target)

    def _branch_message(self, tab, message):
        key, conversation_id = tab["account_key"], tab["conversation_id"]
        tab["message_action_pending"] = True
        self.status = self.tr("Creating branch…")
        self.app.invalidate()

        def work():
            try:
                record = self.account_service.branch_chat(key, conversation_id, message["id"])
            except Exception as error:
                reason = str(error)

                def failed():
                    tab["message_action_pending"] = False
                    self.status = self.tr("Message action failed: {0}").format(reason)
                    self.app.invalidate()

                self._dispatch_ui(failed)
                return

            def ready():
                tab["message_action_pending"] = False
                provider_preview = isinstance(record.get("pending_branch"), dict)
                if provider_preview:
                    self.sidebar_items.insert(0, record)
                    self._invalidate_sidebar_rows()
                count = len(self.tabs)
                self.new_tab()
                if len(self.tabs) > count:
                    self.resume_conversation(record["id"])
                    if not provider_preview:
                        self.tabs[self.active_tab]["_confirm_branch_listing"] = True
                self.status = self.tr("Branch created")
                self.app.invalidate()

            self._dispatch_ui(ready)

        if not self._operations.submit(work):
            tab["message_action_pending"] = False
            self.status = self.tr("Arka plan iş kuyruğu dolu; yeniden deneyin")

    def _confirm_branch_listing(self, tab, conversation_id):
        """Expose a branch only after one provider-list read confirms it."""
        service = self.account_service
        key = tab.get("account_key", self.account_key)
        if service is None or not key or not conversation_id:
            return
        project_id = (tab.get("project") or {}).get("id")
        if project_id:
            callback = service.callbacks(key).get("list_project_chats_callback")
            if callback is None:
                return

            def read():
                return callback(project_id)
        else:
            provider_id = key.split("/", 1)[0]

            def read():
                return service.list_chats(selected={provider_id}, force=True, account_keys={key})[
                    "items"
                ]

        def listed(result):
            items = result.get("items", []) if isinstance(result, dict) else result
            if not isinstance(items, list):
                return
            _, remote = origin(conversation_id, key)
            confirmed = None
            for item in items:
                if not isinstance(item, dict):
                    continue
                value = item.get("remote_id") or item.get("id")
                if not isinstance(value, str):
                    continue
                try:
                    item_key, item_remote = origin(value, key)
                except (TypeError, ValueError):
                    continue
                if item_key == key and item_remote == remote:
                    confirmed = item
                    break
            if confirmed is None:
                return
            current = next(
                (item for item in self.sidebar_items if item.get("id") == confirmed.get("id")),
                None,
            )
            if current is None:
                self.sidebar_items.insert(0, confirmed)
            else:
                current.update(confirmed)
            self.filter_sidebar()

        self._read_in_background(read, listed, lambda _: None, session_bound=False)

    def _submit_message_action(self, tab, message, action, prompt):
        if (
            tab is not self.tabs[self.active_tab]
            or self.active_busy()
            or sum(bool(item.get("busy")) for item in self.tabs + self.background_tabs) >= 4
        ):
            self.status = self.tr("The selected conversation changed; try again")
            self.app.invalidate()
            return
        if action == "edit" and not prompt.strip():
            self.status = self.tr("Message cannot be empty")
            self.app.invalidate()
            return
        index = self._toolbar_message_index(tab["tab_id"], message)
        if index is None:
            return
        self.normalize_model_selection()
        rollback = (tab.get("messages", []), tab.get("history_window"))
        start = index
        if action == "retry":
            while start and self.messages[start - 1].get("role") == "reasoning":
                start -= 1
        messages = list(self.messages[:start])
        if action == "edit":
            messages.append({"role": "user", "text": prompt.strip(), "local_pending": True})
        messages.append({"role": "assistant", "text": "", "local_pending": True})
        self.messages = tab["messages"] = messages
        tab.pop("history_window", None)
        tab["load_token"] = uuid.uuid4().hex
        tab.update(
            busy=True,
            syncing=False,
            started=time.monotonic(),
            cancel_requested=False,
            completed_pending=False,
            connection=None,
        )
        self.active_started = tab["started"]
        self._sync_activity()
        self.status = (
            self.tr("Retrying response…")
            if action == "retry"
            else self.tr("Sending edited message…")
        )
        self.scroll_bottom()
        thread = threading.Thread(
            target=self.run_send,
            args=(
                prompt.strip(),
                [],
                self.active_tab,
                messages,
                tab["conversation_id"],
                self.model,
                self.effort,
                self.current_project.get("id"),
                False,
            ),
            kwargs={
                "send_callback": self.send_prompt,
                "target_tab": tab,
                "message_action": action,
                "message_id": message["id"],
                "rollback": rollback,
            },
            daemon=True,
        )
        thread.start()

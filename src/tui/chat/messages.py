"""MessageComponent: messages behavior for ChatView."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from datetime import datetime

from webai_provider.messages import files_from

from accounts import origin
from utils import clipboard

from ..markdown import (
    message_body_text,
    render_chatgpt_annotations,
    visible_chat_message,
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
            version_count = message.get("_response_version_count", 1)
            if (
                action not in message.get("actions", ())
                or (
                    version_count > 1
                    and message.get("_response_version_index", 1)
                    != message.get("_response_version_current_index", version_count)
                )
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

    def _open_conversation_export(self, action):
        if not self.tabs:
            return
        saving = action == "save_conversation"
        tab_id = self.tabs[self.active_tab].get("tab_id")
        title = self.tr("Save conversation") if saving else self.tr("Copy conversation")
        field = self.tr("Export format")
        choices = [
            ("text", self.tr("Plain text")),
            ("json", self.tr("JSON")),
        ]

        def submit(values):
            export_format = values.get("format")
            if export_format not in ("text", "json"):
                raise ValueError(self.tr("Choose an export format"))
            return lambda: self._export_conversation(tab_id, saving, export_format)

        self.forms.open(
            title,
            [("format", field, "text", choices)],
            submit,
            submit_label="Save" if saving else "Copy",
        )

    def _export_conversation(self, tab_id, saving, export_format):
        tab = next((item for item in self.tabs if item.get("tab_id") == tab_id), None)
        if tab is None:
            return
        if tab is not self.tabs[self.active_tab]:
            self.status = self.tr("This conversation is no longer active")
            self.app.invalidate()
            return
        if tab.get("history_load_pending") and not tab.get("history_load_complete"):
            self.status = self.tr("Conversation history is still syncing. Try again shortly.")
            self.app.invalidate()
            return
        with self.lock:
            live_messages = tuple(dict(message) for message in tab.get("messages", ()))
        account_key = tab.get("account_key", self.account_key)
        conversation_id = tab.get("conversation_id")
        temporary = bool(tab.get("temporary"))
        title = str(tab.get("title") or "Conversation")
        downloads = self.downloads_panel.service() if saving else None
        if saving and downloads is None:
            self.status = self.tr("Conversation downloads are unavailable")
            self.app.invalidate()
            return

        progress = (
            self.tr("Saving conversation…") if saving else self.tr("Preparing conversation copy…")
        )
        self.status = progress
        self.app.invalidate()

        def read():
            messages = self._conversation_export_messages(
                account_key, conversation_id, temporary, live_messages
            )
            chunks = self._conversation_export_chunks(title, messages, export_format)
            if saving:
                extension = "json" if export_format == "json" else "txt"
                filename = f"{title}.{extension}"
                return downloads.save_export(
                    account_key,
                    conversation_id,
                    title,
                    filename,
                    chunks,
                )
            content = "".join(chunks)
            return None if clipboard.copy_text(content) else clipboard.terminal_sequence(content)

        def completed(result):
            if saving:
                self.downloads_panel.update()
                message = self.tr("Conversation saved: {0}").format(result["name"])
            elif result is None:
                message = self.tr("Conversation copied")
            else:
                self.app.output.write_raw(result)
                self.app.output.flush()
                message = self.tr("Sent conversation to the terminal clipboard")
            self._finish_conversation_export(tab_id, progress, message)

        def failed(_reason):
            self._finish_conversation_export(
                tab_id, progress, self.tr("Conversation export failed")
            )

        self._read_in_background(read, completed, failed, session_bound=False)

    def _finish_conversation_export(self, tab_id, progress, message):
        if not self.tabs or self.tabs[self.active_tab].get("tab_id") != tab_id:
            return
        if self.status == progress:
            self.status = message
        else:
            try:
                self.show_temporary_status(message)
            except RuntimeError:
                self.status = message
        self.app.invalidate()

    def _conversation_export_messages(self, account_key, conversation_id, temporary, live_messages):
        """Stream visible SQL history and merge unsaved messages from the active transcript."""
        archive = None
        if (
            self.account_service
            and hasattr(self.account_service, "archive")
            and conversation_id
            and not temporary
        ):
            archive = self.account_service.archive()
        if archive is None:
            yield from self._merge_export_messages((), live_messages)
            return

        key, remote = origin(conversation_id, account_key)
        repository = getattr(archive, "repository", None)
        superseded = (
            repository.superseded_message_ids(key, remote) if repository is not None else set()
        )
        live_by_ordinal, live_by_id = {}, {}
        for message in live_messages:
            ordinal = message.get("_archive_ordinal")
            identity = message.get("id")
            if isinstance(ordinal, int):
                live_by_ordinal.setdefault(ordinal, message)
            if identity:
                live_by_id.setdefault(identity, message)
        consumed_ordinals, consumed_ids = set(), set()
        start = 0
        first_page = True
        history_version = None
        while True:
            page = archive.conversation_window(
                key,
                remote,
                start=start,
                limit=500,
                max_chars=1_048_576,
                visible_only=True,
            )
            if page is None:
                if first_page:
                    yield from self._merge_export_messages((), live_messages)
                    return
                raise RuntimeError("conversation archive changed during export")
            if first_page:
                history_version = page.get("history_version")
            elif page.get("history_version") != history_version:
                raise RuntimeError("conversation changed during export")
            first_page = False
            rows = page.get("messages", ())
            for raw in rows:
                identity = raw.get("id")
                ordinal = raw.get("_archive_ordinal")
                if identity in superseded:
                    continue
                current = live_by_ordinal.get(ordinal) or live_by_id.get(identity)
                if current is not None:
                    consumed_ordinals.add(current.get("_archive_ordinal"))
                    if current.get("id"):
                        consumed_ids.add(current["id"])
                    visible = visible_chat_message(current)
                else:
                    visible = visible_chat_message(raw)
                exported = self._conversation_export_record(visible, raw)
                if exported:
                    yield exported
            window = page.get("history_window", {})
            end = window.get("end", start)
            total = window.get("total", end)
            if not rows or end <= start or end >= total:
                break
            start = end

        for message in live_messages:
            ordinal, identity = message.get("_archive_ordinal"), message.get("id")
            if ordinal in consumed_ordinals or (identity and identity in consumed_ids):
                continue
            if identity in superseded:
                continue
            exported = self._conversation_export_record(visible_chat_message(message), message)
            if exported:
                yield exported

    @staticmethod
    def _merge_export_messages(archived, live_messages):
        """Use live messages when no local conversation archive is available."""
        yield from archived
        for message in live_messages:
            exported = MessageComponent._conversation_export_record(
                visible_chat_message(message), message
            )
            if exported:
                yield exported

    @staticmethod
    def _conversation_export_record(message, source=None):
        if not message or message.get("role") not in ("user", "assistant"):
            return None
        text = render_chatgpt_annotations(message_body_text(message))
        files = message.get("_files")
        if files is None:
            files = files_from(source or message)
        attachments = []
        for item in files:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            attachment = {"name": name}
            media_type = item.get("media_type")
            if isinstance(media_type, str) and media_type:
                attachment["media_type"] = media_type
            size = item.get("size_bytes")
            if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
                attachment["size_bytes"] = size
            attachments.append(attachment)
        if not text and not attachments:
            return None
        result = {"role": message["role"], "text": text}
        if attachments:
            result["attachments"] = attachments
        return result

    def _conversation_export_chunks(self, title, messages, export_format):
        if export_format == "json":
            yield '{\n  "title": '
            yield json.dumps(title, ensure_ascii=False)
            yield ',\n  "messages": ['
            first = True
            for message in messages:
                encoded = json.dumps(message, ensure_ascii=False, indent=2)
                yield ("\n" if first else ",\n") + "\n".join(
                    "    " + line for line in encoded.splitlines()
                )
                first = False
            yield "\n  ]\n}\n"
            return

        heading = " ".join(title.split())
        yield f"╭─ {heading} ─╮\n\n"
        for index, message in enumerate(messages):
            if index:
                yield "\n\n"
            role = self.tr("User") if message["role"] == "user" else self.tr("Assistant")
            text = message["text"].strip()
            yield f"━━━━━━━━━━━━ {index + 1:02d} · {role.upper()} ━━━━━━━━━━━━\n\n{text}"
            if message.get("attachments"):
                details = []
                for item in message["attachments"]:
                    label = item["name"]
                    if item.get("media_type"):
                        label += f" ({item['media_type']})"
                    if "size_bytes" in item:
                        size = item["size_bytes"]
                        label += f" · {size / 1024:.1f} KiB" if size >= 1024 else f" · {size} B"
                    details.append(label)
                separator = "\n\n" if text else ""
                yield f"{separator}{self.tr('Attachments')}: {', '.join(details)}"

    def _show_message_sources(self, message):
        from webai_provider.messages import sources_from

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
                    if (
                        tab.get("conversation_id") != conversation_id
                        or tab.get("account_key") != account_key
                    ):
                        tab.pop("response_version_loading", None)
                        tab.pop("response_version_target", None)
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
                        from ..markdown import visible_chat_messages

                        raw_messages = [dict(item) for item in result["messages"]]
                        primary_message_id = result["message"]["id"]
                        display_ids = []
                        for raw in raw_messages:
                            if raw.get("id") == primary_message_id:
                                raw["id"] = message_id
                                raw["_response_version_index"] = result["index"]
                                raw["_response_version_count"] = result["count"]
                                raw["_response_version_current_index"] = result["current_index"]
                            if raw.get("id"):
                                display_ids.append(raw["id"])
                        alternate_messages = visible_chat_messages(raw_messages)
                        primary_visible = next(
                            (
                                item
                                for item in alternate_messages
                                if item.get("id") == message_id
                                and item.get("_response_version_index") == result["index"]
                            ),
                            None,
                        )
                        if primary_visible is None:
                            error_reason = self.tr("Response version is unavailable")
                        else:
                            primary_visible["_response_variant_message_ids"] = tuple(display_ids)
                            displayed_ids = set(
                                tab["messages"][target_index].get(
                                    "_response_variant_message_ids", result["current_message_ids"]
                                )
                            )
                            insertion_point = next(
                                (
                                    position
                                    for position, item in enumerate(tab["messages"])
                                    if item.get("id") in displayed_ids
                                ),
                                target_index,
                            )
                            retained_before = sum(
                                item.get("id") not in displayed_ids
                                for item in tab["messages"][:insertion_point]
                            )
                            messages = [
                                item
                                for item in tab["messages"]
                                if item.get("id") not in displayed_ids
                            ]
                            messages[retained_before:retained_before] = alternate_messages
                            tab["messages"] = messages
                    if error_reason is None and tab is self.tabs[self.active_tab]:
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
            prompt_index = next(
                (
                    position
                    for position in range(index - 1, -1, -1)
                    if tab["messages"][position].get("role") == "user"
                ),
                None,
            )
            if prompt_index is None:
                self.status = self.tr("The selected conversation changed; try again")
                self.app.invalidate()
                return
            start = prompt_index + 1
        messages = list(tab["messages"][:start])
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

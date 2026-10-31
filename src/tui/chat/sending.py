"""SendingComponent: sending behavior for ChatView."""

from __future__ import annotations

import threading
import time
from typing import Any

from accounts import origin
from providers.timing import SendTiming, collect_send_timing, timing_enabled, use_send_timing

from ..markdown import (
    visible_chat_message,
    visible_chat_messages,
)
from ..styles import ICONS
from .contracts import Message
from .stream import _SendStream


class SendingComponent:
    def _apply_send_snapshot(
        self, result: dict[str, Any], messages: list[Message], rendered: str
    ) -> tuple[str | None, str | None]:
        conversation = result.get("conversation")
        if not isinstance(conversation, dict):
            return None, None

        resolved_title = conversation.get("title") or None
        resolved_model = conversation.get("default_model_slug")
        if not isinstance(resolved_model, str) or not resolved_model:
            resolved_model = None
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
            if author.get("role") != "assistant" or message.get("recipient") not in (None, "all"):
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
        return resolved_title, resolved_model

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
        target_tab=None,
        message_action=None,
        message_id=None,
        rollback=None,
        timing: SendTiming | None = None,
    ) -> None:
        if timing is not None:
            timing.record_elapsed("submit_to_send_worker_start_ms", timing.started_ns)
        target_tab = self.tabs[tab_index] if target_tab is None else target_tab

        def is_active():
            return target_tab is self.tabs[self.active_tab]

        stream = _SendStream(self, target_tab, messages, timing=timing)
        started = target_tab.get("started", self.active_started)
        elapsed = None
        final_status = "Ready"
        send_succeeded = False
        submitted_attachment_ids = {
            str(item["queue_id"]) for item in attachments if item.get("queue_id")
        }
        resolved_id = conversation_id
        resolved_title: str | None = None
        resolved_model: str | None = None

        try:
            if timing is None:
                result = (send_callback or self.send_prompt)(
                    conversation_id,
                    prompt,
                    attachments,
                    stream.on_event,
                    stream.register_connection,
                    model,
                    effort,
                    **({"project_id": project_id} if project_id else {}),
                    **({"temporary": True} if temporary else {}),
                    **(
                        {"action": message_action, "message_id": message_id}
                        if message_action
                        else {}
                    ),
                )
            else:
                with use_send_timing(timing):
                    result = (send_callback or self.send_prompt)(
                        conversation_id,
                        prompt,
                        attachments,
                        stream.on_event,
                        stream.register_connection,
                        model,
                        effort,
                        **({"project_id": project_id} if project_id else {}),
                        **({"temporary": True} if temporary else {}),
                        **(
                            {"action": message_action, "message_id": message_id}
                            if message_action
                            else {}
                        ),
                    )
            send_succeeded = True
            omitted_count = int(result.get("attachment_omitted_count") or 0)
            if omitted_count:
                final_status = self.tr(
                    "Yanıt tamamlandı · {0} dosya gönderilemedi ve listeden kaldırıldı"
                ).format(omitted_count)
            if result.get("cache_warning"):
                final_status = result["cache_warning"]
            result_id = result.get("conversation_id")
            if isinstance(result_id, str) and result_id:
                resolved_id = result_id
            resolved_title, resolved_model = self._apply_send_snapshot(
                result, messages, stream.rendered
            )
        except Exception as error:
            error_code = getattr(error, "code", None)
            message_submitted = getattr(error, "message_submitted", None)
            if error_code == "chatgpt_sentinel_incompatible":
                failure_message = self.tr(
                    "ChatGPT verification is incompatible; the message was not sent"
                )
                final_status = self.tr("Message not sent")
            elif message_submitted is False:
                failure_message = self.tr("Message not sent: {0}").format(error)
                final_status = self.tr("Message not sent")
            else:
                failure_message = self.tr("Request stopped: {0}").format(error)
                final_status = self.tr("Response stopped")
            if not stream.rendered:
                with self.lock:
                    if messages and messages[-1].get("role") == "assistant":
                        messages[-1]["text"] = "*" + failure_message + "*"
            if message_action and message_submitted is not False:
                final_status = self.tr("Message action failed: {0}").format(error)
        finally:
            if started is not None:
                elapsed = time.monotonic() - started

            def finish_turn() -> None:
                nonlocal messages
                if message_action and not send_succeeded and rollback is not None:
                    messages = rollback[0]
                    if rollback[1] is not None:
                        target_tab["history_window"] = rollback[1]
                elif send_succeeded and result.get("history"):
                    history = result["history"]
                    messages = visible_chat_messages(history.get("messages", ()))
                    if history.get("history_window"):
                        target_tab["history_window"] = history["history_window"]
                if send_succeeded and result.get("replaced_conversation_id"):
                    previous_id = result["replaced_conversation_id"]
                    self.sidebar_items = [
                        item for item in self.sidebar_items if item.get("id") != previous_id
                    ]
                    for tab in self.tabs:
                        if tab is not target_tab and tab.get("conversation_id") == previous_id:
                            tab["conversation_id"] = resolved_id
                            tab["loaded"] = False
                if attachments:
                    submitted_ids = submitted_attachment_ids
                    self.pending_attachments = [
                        item
                        for item in self.pending_attachments
                        if str(item.get("queue_id") or "") not in submitted_ids
                    ]
                    if send_succeeded:
                        self._release_staged_attachments(attachments)
                    else:
                        queued_ids = {
                            str(item.get("queue_id") or "") for item in self.pending_attachments
                        }
                        restored = [
                            item
                            for item in attachments
                            if str(item.get("queue_id") or "") not in queued_ids
                        ]
                        self.pending_attachments = restored + self.pending_attachments
                    self._attachments_in_flight = False
                    self._inflight_attachments = []
                tab = target_tab
                tab["temporary"] = temporary
                tab["conversation_id"] = resolved_id
                tab["title"] = resolved_title or tab.get("title") or self.tr("New conversation")
                tab["messages"] = messages
                tab["render_revision"] = tab.get("render_revision", 0) + 1
                if tab.get("model") == model or not tab.get("model"):
                    tab["model"] = model or resolved_model or ""
                tab["response_model"] = resolved_model
                if tab.get("effort") == effort:
                    tab["effort"] = effort
                tab["generation_complete"] = False
                tab["busy"] = False
                tab["stream_received"] = False
                tab["completed_pending"] = not is_active()
                tab["started"] = None
                tab["connection"] = None
                tab["cancel_requested"] = False
                queued = tab.pop("queued_submit", None)
                queued_to_start = queued if queued is not None and send_succeeded else None
                if queued is not None:
                    if not send_succeeded:
                        if is_active():
                            self.input.text = queued["prompt"]
                        else:
                            tab["draft"] = queued["prompt"]
                        existing_attachment_ids = {
                            str(item.get("queue_id") or "") for item in self.pending_attachments
                        }
                        restored = [
                            item
                            for item in queued["attachments"]
                            if str(item.get("queue_id") or "") not in existing_attachment_ids
                        ]
                        self.pending_attachments = restored + self.pending_attachments
                confirm_branch_listing = bool(
                    send_succeeded and tab.pop("_confirm_branch_listing", False)
                )
                if resolved_id and not temporary:
                    if not confirm_branch_listing:
                        record = next(
                            (item for item in self.sidebar_items if item.get("id") == resolved_id),
                            None,
                        )
                        if record is None:
                            record = {"id": resolved_id}
                            self.sidebar_items.insert(0, record)
                        record.update(
                            title=tab["title"],
                            provider_id=tab.get("account_key", self.account_key).split("/")[0],
                            project_id=(tab.get("project") or {}).get("id"),
                        )
                self._invalidate_sidebar_rows()
                if confirm_branch_listing:
                    self._confirm_branch_listing(tab, resolved_id)
                if (
                    queued_to_start is None
                    and any(item is tab for item in self.background_tabs)
                    and send_succeeded
                    and (temporary or self.archive_service())
                ):
                    if temporary or not result.get("cache_warning"):
                        self.background_tabs = [
                            item for item in self.background_tabs if item is not tab
                        ]
                        tab["messages"] = []
                        if temporary and resolved_id:
                            forget = (
                                self.account_service.callbacks(
                                    tab.get("account_key", self.account_key)
                                ).get("forget_temporary")
                                if self.account_service
                                else self.forget_temporary
                            )
                            if forget:
                                self._read_in_background(
                                    lambda: forget(resolved_id),
                                    lambda _: None,
                                    lambda _: None,
                                    session_bound=False,
                                )
                        if self.archive_service() and resolved_id:
                            key, remote = origin(
                                resolved_id, tab.get("account_key", self.account_key)
                            )
                            self.archive_service().release(key, remote)
                self._sync_activity()
                if is_active():
                    self.stop_confirmation_until = 0.0
                    self.stop_confirmation_tab_id = None
                    self.conversation_id = resolved_id
                    self.title = tab["title"]
                    self.messages = messages
                    self.model = tab["model"]
                    self.last_response_model = resolved_model
                    self.effort = tab["effort"]
                    self.last_elapsed = elapsed
                    self.status = final_status
                    if self.set_active_conversation is not None:
                        self.set_active_conversation(None if tab.get("temporary") else resolved_id)
                    self._save_active_tab()
                    self.scroll_bottom(force=False)
                    if send_succeeded and not temporary and self.archive_service():
                        tab.setdefault("history_window", {"start": 0, "end": 0, "total": 0})
                        self.history_pager.request("bottom", refresh=True)
                else:
                    self.status = (
                        self.tr("Yanıt tamamlandı: {0}").format(tab["title"])
                        if send_succeeded
                        else final_status
                    )
                    self.app.invalidate()
                if any(item is tab for item in self.tabs):
                    self._persist_session()
                if queued_to_start is not None:
                    self._execute_queued_submit(tab, queued_to_start)

            self._dispatch_ui(finish_turn)
            if timing is not None:
                collect_send_timing(timing)

    def _execute_queued_submit(self, tab: dict[str, Any], queued: dict[str, Any]) -> None:
        prompt = queued["prompt"] or self.tr("Please review the attached files.")
        attachments = queued["attachments"]
        model = queued["model"]
        effort = queued["effort"]
        project_id = queued["project_id"]
        temporary = queued["temporary"]
        display_prompt = prompt
        if attachments:
            labels = " ".join(
                f"[{ICONS['attachment'].icon} {item['name']}]" for item in attachments
            )
            display_prompt = f"{display_prompt}\n\n{labels}"
        messages = tab.setdefault("messages", [])
        with self.lock:
            messages.append({"role": "user", "text": display_prompt, "local_pending": True})
            messages.append({"role": "assistant", "text": "", "local_pending": True})
        tab_index = next((i for i, item in enumerate(self.tabs) if item is tab), -1)
        tab["busy"] = True
        tab["stream_received"] = False
        tab["cancel_requested"] = False
        tab["started"] = time.monotonic()
        tab["completed_pending"] = False
        tab["draft"] = ""
        tab["empty_workspace"] = False
        if attachments:
            self._attachments_in_flight = True
            self._inflight_attachments = list(attachments)
        self.stop_confirmation_until = 0.0
        self.stop_confirmation_tab_id = None
        self.last_elapsed = None
        self._invalidate_sidebar_rows()
        self._sync_activity()
        if tab is self.tabs[self.active_tab]:
            self.status = self.tr("Yanıt bekleniyor…")
            self.scroll_bottom()
            self._save_active_tab()
        self.app.invalidate()
        timing = (
            SendTiming()
            if tab.get("account_key", self.account_key).startswith("chatgpt") and timing_enabled()
            else None
        )
        threading.Thread(
            target=self.run_send,
            kwargs={
                "send_callback": self.send_prompt,
                "target_tab": tab,
                "timing": timing,
            },
            args=(
                prompt,
                attachments,
                tab_index if tab_index >= 0 else self.active_tab,
                messages,
                tab.get("conversation_id"),
                model,
                effort,
                project_id,
                temporary,
            ),
            daemon=True,
        ).start()

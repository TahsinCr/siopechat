"""Provider stream event handling for one conversation tab (supports multi-part Response.parts)."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any

from webaiprovider.core.timing import SendTiming

if TYPE_CHECKING:
    from .view import ChatView

from ..markdown import extract_message_files, message_body_text, visible_chat_message
from .contracts import Message


class _SendStream:
    """Apply synchronous or async provider stream events to one conversation tab."""

    def __init__(
        self,
        view: ChatView,
        target_tab: dict[str, Any],
        messages: list[Message],
        timing: SendTiming | None = None,
    ):
        self.view = view
        self.target_tab = target_tab
        self.messages = messages
        self.timing = timing
        self.stream_id = uuid.uuid4().hex
        self.target_tab["_active_stream_id"] = self.stream_id
        self.target_tab.pop("_first_text_render_revision", None)
        self.rendered = ""
        self._final_pending: list[str] = []
        self._final_replace_pending = False
        self._reasoning_pending: dict[str, tuple[Message, list[str], bool]] = {}
        self._closed_final_parts: set[str] = set()
        self._last_refresh_ns = 0
        self.final_message_id = None
        self._first_final_text_at_ns = None
        self._first_render_revision = None
        self._after_render_registered = False
        self._ui_first_text_recorded = False
        self._stream_finished = False
        self._refresh_lock = threading.Lock()
        self._refresh_pending = False
        self._refresh_revision = 0
        self._refresh_status: str | None = None
        self._refresh_complete = False

    def _is_active(self) -> bool:
        return self.target_tab is self.view.tabs[self.view.active_tab]

    def _schedule_refresh(
        self,
        *,
        show_status: bool = False,
        status_message: str | None = None,
        mark_complete: bool = False,
        immediate: bool = False,
    ) -> None:
        with self._refresh_lock:
            if status_message is not None:
                self._refresh_status = status_message
            elif show_status and self._refresh_status is None:
                self._refresh_status = "Streaming response…"
            self._refresh_complete = self._refresh_complete or mark_complete
            if self._refresh_pending and not immediate:
                return
            self._refresh_pending = True
            self._refresh_revision += 1
            revision = self._refresh_revision

        def update_active_view() -> None:
            with self._refresh_lock:
                if revision != self._refresh_revision:
                    return
                delay_ns = 16_000_000 - (time.perf_counter_ns() - self._last_refresh_ns)
                if not immediate and self._last_refresh_ns and delay_ns > 0:
                    try:
                        asyncio.get_running_loop().call_later(
                            delay_ns / 1_000_000_000, update_active_view
                        )
                        return
                    except RuntimeError:
                        pass
                update_status = self._refresh_status
                self._refresh_status = None
                update_complete = self._refresh_complete
                self._refresh_complete = False
                self._refresh_pending = False
            self.flush_pending_text()
            if immediate:
                render_revision = self.target_tab.get("render_revision", 0)
                self._first_render_revision = render_revision
                if self._is_active():
                    self.target_tab["_first_text_render_revision"] = render_revision
            self._last_refresh_ns = time.perf_counter_ns()
            if (
                update_complete
                and self.target_tab.get("busy")
                and self.target_tab.get("_active_stream_id") == self.stream_id
            ):
                self.target_tab["generation_complete"] = True
                self.target_tab["completed_pending"] = True
            elif update_complete:
                update_status = None
            if update_complete:
                self.view.app.invalidate()
            if (
                self.timing is not None
                and self._first_final_text_at_ns is not None
                and not self._after_render_registered
                and not self._ui_first_text_recorded
                and self._is_active()
            ):
                self.view.app.after_render += self._after_first_text_render
                self._after_render_registered = True
            if self._is_active():
                if update_status:
                    self.view.status = self.view.tr(update_status)
                self.view.scroll_bottom(force=False)

        self.view._dispatch_ui(update_active_view)

    def _active_assistant_index(self) -> int:
        for idx in range(len(self.messages) - 1, -1, -1):
            if self.messages[idx].get("role") == "assistant":
                return idx
        return len(self.messages)

    def _flush_final_unlocked(self) -> bool:
        if not self._final_pending:
            return False
        addition = "".join(self._final_pending)
        self.rendered = addition if self._final_replace_pending else self.rendered + addition
        self._final_pending.clear()
        self._final_replace_pending = False
        idx = self._active_assistant_index()
        if idx < len(self.messages) and self.messages[idx].get("text") != self.rendered:
            self.messages[idx]["text"] = self.rendered
            return True
        return False

    def flush_pending_text(self) -> None:
        changed = False
        with self.view.lock:
            changed = self._flush_final_unlocked()
            for item, chunks, replaces in self._reasoning_pending.values():
                addition = "".join(chunks)
                new_text = addition if replaces else item["text"] + addition
                if item["text"] != new_text:
                    item["text"] = new_text
                    changed = True
            self._reasoning_pending.clear()
            if changed:
                self.target_tab["render_revision"] = self.target_tab.get("render_revision", 0) + 1

    def register_connection(self, connection: Any) -> None:
        self.target_tab["connection"] = connection
        self.target_tab["job_id"] = getattr(connection, "id", None)
        if self._is_active():
            self.view.cancel_connection = connection
        if self.target_tab.get("cancel_requested"):
            self.view._cancel_connection(connection)

    def _start_next_final_part_unlocked(self, identity: str | None) -> None:
        self._flush_final_unlocked()
        if (
            self.final_message_id
            and self.final_message_id in self._closed_final_parts
            and self.rendered
        ):
            # Start a fresh assistant block instead of overwriting the completed prior part.
            insert_at = self._active_assistant_index() + 1
            self.messages.insert(
                insert_at,
                {
                    "role": "assistant",
                    "text": "",
                    "stream_id": identity,
                    "local_pending": True,
                },
            )
            self.rendered = ""
            self._final_pending.clear()
            self._final_replace_pending = True

    def on_event(self, event: dict[str, Any]) -> None:
        self.target_tab["stream_received"] = True
        connection = self.target_tab.get("connection")
        if connection is not None:
            self.target_tab["job_id"] = getattr(connection, "id", None)
        event_type = event.get("type")
        if event_type == "bound":
            cid = event.get("conversation_id")
            if cid:
                account_key = self.target_tab.get("account_key") or getattr(
                    self.view, "account_key", ""
                )
                if account_key and not str(cid).startswith("@"):
                    from accounts import identity

                    cid = identity(account_key, cid)
                self.target_tab["conversation_id"] = cid
                if self._is_active():
                    self.view.conversation_id = cid
                    if self.view.set_active_conversation is not None:
                        self.view.set_active_conversation(
                            None if self.target_tab.get("temporary") else cid
                        )
                self.view._invalidate_sidebar_rows()
                self.view.app.invalidate()
            return
        if event_type == "response_complete":
            self.flush_pending_text()
            started = self.target_tab.get("started")
            if started is not None:
                self.target_tab["generation_elapsed"] = max(0.0, time.monotonic() - started)
            self._schedule_refresh(status_message="Response complete", mark_complete=True)
            return
        if event_type == "verifying":
            self._schedule_refresh(status_message="Verifying session…")
            return
        if event_type == "parts_resolved":
            return
        if event_type == "metadata":
            self.flush_pending_text()
            with self.view.lock:
                idx = self._active_assistant_index()
                if idx < len(self.messages):
                    final = self.messages[idx]
                    if event.get("message_id"):
                        final["id"] = event["message_id"]
                    final["actions"] = list(event.get("actions", ()))
                    if event.get("sources"):
                        final["_thinking_sources"] = tuple(event["sources"])
                    final["_response_settings"] = tuple(
                        (label, event[key])
                        for label, key in (("Model", "model"), ("Effort", "effort"))
                        if event.get(key)
                    )
                    source_text = message_body_text(final)
                    files = extract_message_files(
                        {"text": source_text, "files": event.get("files", ())}
                    )
                    if files:
                        prepared = visible_chat_message(
                            {"role": "assistant", "text": source_text, "files": files}
                        )
                        final.update(
                            {key: prepared[key] for key in ("text", "_body_length", "_files")}
                        )
                if event.get("user_message_id"):
                    user = next(
                        (
                            item
                            for item in reversed(self.messages[:-1])
                            if item.get("role") == "user"
                        ),
                        None,
                    )
                    if user is not None:
                        user["id"] = event["user_message_id"]
                        user["actions"] = [] if self.target_tab.get("temporary") else ["edit"]
                self.target_tab["render_revision"] = self.target_tab.get("render_revision", 0) + 1
            self._schedule_refresh()
            return

        if event.get("channel") == "analysis" or event_type == "reasoning":
            part_key = str(event.get("part_id") or event.get("message_id") or "reasoning")
            identity = self.stream_id + ":analysis:" + part_key
            if event_type == "begin":
                with self.view.lock:
                    if not any(item.get("stream_id") == identity for item in self.messages):
                        insert_at = self._active_assistant_index()
                        if insert_at < len(self.messages) and self.messages[insert_at].get("text"):
                            insert_at = len(self.messages)
                        self.messages.insert(
                            max(0, insert_at),
                            {
                                "role": "reasoning",
                                "text": "",
                                "stream_id": identity,
                                "local_pending": True,
                            },
                        )
                return
            if event_type == "end":
                self._schedule_refresh()
                return
            text = event.get("text")
            if not isinstance(text, str):
                return
            with self.view.lock:
                item = next(
                    (item for item in self.messages if item.get("stream_id") == identity), None
                )
                if item is None:
                    insert_at = self._active_assistant_index()
                    if insert_at < len(self.messages) and self.messages[insert_at].get("text"):
                        insert_at = len(self.messages)
                    item = {
                        "role": "reasoning",
                        "text": "",
                        "stream_id": identity,
                        "local_pending": True,
                    }
                    self.messages.insert(max(0, insert_at), item)
                pending = self._reasoning_pending.get(identity)
                if pending is None or pending[0] is not item:
                    pending = (item, [], False)
                    self._reasoning_pending[identity] = pending
                chunks = pending[1]
                if event_type == "delta":
                    chunks.append(text)
                else:
                    chunks[:] = [text]
                    self._reasoning_pending[identity] = (item, chunks, True)
            self._schedule_refresh()
            return

        identity = event.get("part_id") or event.get("message_id")
        if event_type == "begin":
            with self.view.lock:
                if identity and identity != self.final_message_id:
                    self._start_next_final_part_unlocked(identity)
                    self.final_message_id = identity
            return
        if event_type == "end":
            if identity:
                self._closed_final_parts.add(identity)
            self._schedule_refresh()
            return

        text = event.get("text")
        if event_type not in {"delta", "replace"} or not isinstance(text, str):
            return
        with self.view.lock:
            if identity and self.final_message_id and identity != self.final_message_id:
                if self.final_message_id in self._closed_final_parts:
                    self._start_next_final_part_unlocked(identity)
                else:
                    self.rendered = ""
                    self._final_pending.clear()
                    self._final_replace_pending = True
            if identity:
                self.final_message_id = identity
            if event_type == "delta":
                self._final_pending.append(text)
            else:
                self._final_pending[:] = [text]
                self._final_replace_pending = True
            first_visible = (
                self._first_final_text_at_ns is None
                and event.get("channel") != "analysis"
                and bool(text.strip())
            )
            if first_visible:
                self._first_final_text_at_ns = time.perf_counter_ns()
        self._schedule_refresh(show_status=True, immediate=first_visible)

    def _after_first_text_render(self, app) -> None:
        if self._ui_first_text_recorded:
            return
        if not self._is_active() or not self.rendered:
            if self._stream_finished:
                self.cleanup_after_render()
            return
        if app.is_running:
            signature = getattr(self.view, "_virtual_signature", None)
            if (
                signature is None
                or signature[0] != self.target_tab["tab_id"]
                or signature[5][0] != id(self.messages)
                or self._first_render_revision is None
                or signature[7] < self._first_render_revision
            ):
                return
            info = self.view.transcript_window.render_info
            rows = self.view.transcript_control.rows
            layout = getattr(self.view, "_virtual_transcript", None)
            if info is None or layout is None or getattr(rows, "transcript", None) is not layout:
                return
            index = self._active_assistant_index()
            for line in info.displayed_lines:
                row = rows.paint_row(line)
                if row is not None and row[1] == index and any(span[1].strip() for span in row[0]):
                    break
            else:
                return
        now = time.perf_counter_ns()
        self._ui_first_text_recorded = True
        if self.timing is not None:
            self.timing.record("button_to_first_ui_frame_ms", now - self.timing.started_ns)
            if self._first_final_text_at_ns is not None:
                self.timing.record(
                    "first_sse_text_to_ui_frame_ms",
                    now - self._first_final_text_at_ns,
                )
        app.after_render -= self._after_first_text_render
        self._after_render_registered = False

    def cleanup_after_render(self) -> None:
        self._stream_finished = True
        if (
            self._after_render_registered
            and not self._ui_first_text_recorded
            and self._is_active()
            and self.rendered
        ):
            return
        if self._after_render_registered:
            self.view.app.after_render -= self._after_first_text_render
            self._after_render_registered = False

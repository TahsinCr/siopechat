"""Provider stream event handling for one conversation tab."""

from __future__ import annotations

import threading
import time
import uuid
from typing import TYPE_CHECKING, Any

from providers.timing import SendTiming

if TYPE_CHECKING:
    from .view import ChatView

from ..markdown import message_body_text, visible_chat_message
from .contracts import Message


class _SendStream:
    """Apply provider stream events to one conversation tab."""

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
        self.rendered = ""
        self.final_message_id = None
        self._first_final_text_at_ns = None
        self._after_render_registered = False
        self._ui_first_text_recorded = False
        self._refresh_lock = threading.Lock()
        self._refresh_pending = False
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
    ) -> None:
        with self._refresh_lock:
            if status_message is not None:
                self._refresh_status = status_message
            elif show_status and self._refresh_status is None:
                self._refresh_status = "Streaming response…"
            self._refresh_complete = self._refresh_complete or mark_complete
            if self._refresh_pending:
                return
            self._refresh_pending = True

        def update_active_view() -> None:
            with self._refresh_lock:
                update_status = self._refresh_status
                self._refresh_status = None
                update_complete = self._refresh_complete
                self._refresh_complete = False
                self._refresh_pending = False
            if update_complete and self.target_tab.get("busy"):
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
                and self._is_active()
            ):
                self.view.app.after_render += self._after_first_text_render
                self._after_render_registered = True
            if self._is_active():
                if update_status:
                    self.view.status = self.view.tr(update_status)
                self.view.scroll_bottom(force=False)

        self.view._dispatch_ui(update_active_view)

    def register_connection(self, connection: Any) -> None:
        self.target_tab["connection"] = connection
        self.target_tab["job_id"] = getattr(connection, "id", None)
        if self._is_active():
            self.view.cancel_connection = connection
        if self.target_tab.get("cancel_requested"):
            self.view._cancel_connection(connection)

    def on_event(self, event: dict[str, Any]) -> None:
        self.target_tab["stream_received"] = True
        connection = self.target_tab.get("connection")
        if connection is not None:
            self.target_tab["job_id"] = getattr(connection, "id", None)
        event_type = event.get("type")
        if event_type == "response_complete":
            started = self.target_tab.get("started")
            if started is not None:
                self.target_tab["generation_elapsed"] = max(0.0, time.monotonic() - started)
            self._schedule_refresh(status_message="Response complete", mark_complete=True)
            return
        if event_type == "metadata":
            with self.view.lock:
                final = self.messages[-1]
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
                from providers.messages import files_from

                source_text = message_body_text(final)
                files = files_from({"text": source_text, "files": event.get("files", ())})
                if files:
                    prepared = visible_chat_message(
                        {"role": "assistant", "text": source_text, "files": files}
                    )
                    final.update({key: prepared[key] for key in ("text", "_body_length", "_files")})
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
            text = event.get("text")
            if not isinstance(text, str):
                return
            identity = self.stream_id + ":" + str(event.get("message_id") or "reasoning")
            with self.view.lock:
                item = next(
                    (item for item in self.messages if item.get("stream_id") == identity), None
                )
                if item is None:
                    item = {
                        "role": "reasoning",
                        "text": "",
                        "stream_id": identity,
                        "local_pending": True,
                    }
                    self.messages.insert(max(0, len(self.messages) - 1), item)
                item["text"] = item["text"] + text if event_type == "delta" else text
                self.target_tab["render_revision"] = self.target_tab.get("render_revision", 0) + 1
            self._schedule_refresh()
            return

        identity = event.get("message_id")
        if identity and self.final_message_id and identity != self.final_message_id:
            self.rendered = ""
        if identity:
            self.final_message_id = identity
        text = event.get("text")
        if event_type == "delta" and isinstance(text, str):
            self.rendered += text
        elif event_type == "replace" and isinstance(text, str):
            self.rendered = text
        else:
            return
        with self.view.lock:
            self.messages[-1]["text"] = self.rendered
            self.target_tab["render_revision"] = self.target_tab.get("render_revision", 0) + 1
        if (
            self.timing is not None
            and self._first_final_text_at_ns is None
            and event.get("channel") != "analysis"
            and text.strip()
        ):
            self._first_final_text_at_ns = time.perf_counter_ns()
        self._schedule_refresh(show_status=True)

    def _after_first_text_render(self, app) -> None:
        if self._ui_first_text_recorded or not self._is_active() or not self.rendered:
            return
        now = time.perf_counter_ns()
        self._ui_first_text_recorded = True
        self.timing.record_elapsed("button_to_first_ui_frame_ms", self.timing.started_ns)
        self.timing.record(
            "first_sse_text_to_ui_frame_ms",
            now - self._first_final_text_at_ns,
        )
        app.after_render -= self._after_first_text_render

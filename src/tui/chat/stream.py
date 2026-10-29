"""Provider stream event handling for one conversation tab."""

from __future__ import annotations

import threading
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .view import ChatView

from ..markdown import message_body_text, visible_chat_message
from .contracts import Message


class _SendStream:
    """Apply provider stream events to one conversation tab."""

    def __init__(self, view: ChatView, target_tab: dict[str, Any], messages: list[Message]):
        self.view = view
        self.target_tab = target_tab
        self.messages = messages
        self.stream_id = uuid.uuid4().hex
        self.rendered = ""
        self.final_message_id = None
        self._refresh_lock = threading.Lock()
        self._refresh_pending = False
        self._refresh_status = False

    def _is_active(self) -> bool:
        return self.target_tab is self.view.tabs[self.view.active_tab]

    def _schedule_refresh(self, *, show_status: bool = False) -> None:
        with self._refresh_lock:
            self._refresh_status = self._refresh_status or show_status
            if self._refresh_pending:
                return
            self._refresh_pending = True

        def update_active_view() -> None:
            with self._refresh_lock:
                update_status = self._refresh_status
                self._refresh_status = False
                self._refresh_pending = False
            if self._is_active():
                if update_status:
                    self.view.status = self.view.tr("Streaming response…")
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
        connection = self.target_tab.get("connection")
        if connection is not None:
            self.target_tab["job_id"] = getattr(connection, "id", None)
        event_type = event.get("type")
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
        self._schedule_refresh(show_status=True)

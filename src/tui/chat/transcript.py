"""TranscriptComponent: transcript behavior for ChatView."""

from __future__ import annotations

import asyncio
import shutil
import time
from io import StringIO

from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.utils import get_cwidth
from rich.align import Align
from rich.console import Console
from rich.text import Text

from ..history import (
    prepare_transcript,
    prepare_virtual_transcript,
)
from ..markdown import (
    DEFAULT_MARKDOWN_THEME,
    index_transcript,
    transcript_fragments,
)
from ..styles import (
    DEFAULT_COMPOSER_MAX_LINES,
    DEFAULT_MESSAGE_WIDTH,
    DEFAULT_TRANSCRIPT_GUTTER_WIDTH,
    ICONS,
)


class TranscriptComponent:
    @staticmethod
    def format_elapsed(seconds: float) -> str:
        total = max(0, int(seconds))
        if total < 60:
            return f"{total}s"
        return f"{total // 60}m {total % 60:02d}s"

    def render_activity(self) -> FormattedText:
        stop_hint = self.tr("Yanıtı durdurmak için Esc'ye tekrar basın · 3 sn")
        if self.active_busy() and self.stop_confirmation_tab_id == self.tabs[self.active_tab].get(
            "tab_id"
        ):
            if time.monotonic() <= self.stop_confirmation_until:
                return FormattedText([("", f" {stop_hint} ")])
            self.stop_confirmation_until = 0.0
            self.stop_confirmation_tab_id = None
        if self.busy and self.busy_tab_index is None:
            return FormattedText([("class:sidebar.running", self.tr("Yanıt arka planda sürüyor"))])
        if self.busy and self.busy_tab_index != self.active_tab:
            running = (self.busy_tab_index or 0) + 1
            return FormattedText(
                [
                    (
                        "",
                        f" {ICONS['running'].icon} "
                        + self.tr(
                            "Yanıt {0}. sekmede sürüyor · diğer sekmelere geçebilirsiniz"
                        ).format(running)
                        + " ",
                    )
                ]
            )
        started = (
            self.active_started
            if self.busy and self.busy_tab_index == self.active_tab
            else self.loading_started
            if self.loading_conversation
            else self.upload_started
        )
        if started is not None:
            frames = ICONS["spinner"].icon
            frame = frames[int((time.monotonic() - started) * 10) % len(frames)]
            elapsed = self.format_elapsed(time.monotonic() - started)
            phase = (
                self.tr("Yanıt üretiliyor")
                if self.busy
                else self.tr("Sohbet yükleniyor")
                if self.loading_conversation
                else self.tr("Dosya yükleniyor")
            )
            detail = "" if self.status == stop_hint else " · " + self.status
            return FormattedText([("", f" {frame} {phase} · {elapsed}{detail} ")])
        archive = self.archive_service()
        if archive:
            states = self.sync_states(archive)
            pending_sync = [x for x in states if x["state"] == "syncing"]
            failures = [x for x in states if x["state"] == "error"]
            if pending_sync:
                return FormattedText(
                    [
                        (
                            "class:sync",
                            f" {ICONS['retry'].icon} "
                            + self.tr("Senkronizasyon · {0} kayıt · {1}").format(
                                len(pending_sync), self.status
                            )
                            + " ",
                        )
                    ]
                )
            if failures:
                detail = self.sync_failure_detail(failures)
                if detail:
                    return FormattedText(
                        [
                            (
                                "class:sync.error",
                                self.tr(" ! {0} kayıt · {1} · önbellek korunuyor ").format(
                                    len(failures), detail
                                ),
                            )
                        ]
                    )
                return FormattedText(
                    [
                        (
                            "class:sync.error",
                            self.tr(
                                " ! {0} kayıt senkronize edilemedi · önbellek korunuyor "
                            ).format(len(failures)),
                        )
                    ]
                )
        pending = [
            label
            for label, active in (
                (self.tr("Modeller"), self.catalog_loading),
                (self.tr("Sohbetler"), self.sidebar_loading),
                (self.tr("Projeler"), self.projects_loading),
            )
            if active
        ]
        if pending:
            status = self.status if self.status not in ("Ready", "") else self.tr("Hazır")
            return FormattedText(
                [("", self.tr(" • {0} · {1} yenileniyor").format(status, ", ".join(pending)))]
            )
        if self.status not in ("Ready", ""):
            return FormattedText([("", f" • {self.status} ")])
        if self.pending_attachments:
            return FormattedText(
                [
                    (
                        "",
                        f" {ICONS['attachment'].icon} "
                        + self.tr("{0} dosya hazır").format(len(self.pending_attachments))
                        + " ",
                    )
                ]
            )
        if self.last_elapsed is not None:
            return FormattedText(
                [
                    (
                        "",
                        self.tr(" — Worked for {0} ").format(
                            self.format_elapsed(self.last_elapsed)
                        ),
                    ),
                ]
            )
        return FormattedText([("", self.tr(" Hazır"))])

    def remember_transcript(
        self,
        tab,
        messages,
        width,
        labels,
        reasoning,
        text,
        ranges,
        ansi,
        lines=None,
        *,
        message_key=None,
        revision=0,
    ):
        cache = getattr(self, "_prepared_transcripts", {})
        self._prepared_transcripts = cache
        key = tab["tab_id"]
        cache.pop(key, None)
        roles = tuple(message["role"] for message in messages)
        cost = (
            len(text.encode("utf-8"))
            + sum(len(part[1].encode("utf-8")) for part in ansi)
            + len(roles) * 8
        )
        budget = 1_000_000
        if cost > budget:
            return
        cache[key] = dict(
            width=width,
            conversation_id=tab.get("conversation_id"),
            labels=labels,
            reasoning=reasoning,
            message_key=message_key or self._virtual_message_key(tab.get("messages", [])),
            revision=revision,
            roles=roles,
            text=text,
            ranges=ranges,
            ansi=ansi,
            lines=lines if lines is not None else index_transcript(ansi, ranges),
            cost=cost,
        )
        while cache and sum(item["cost"] for item in cache.values()) > budget:
            cache.pop(next(iter(cache)))

    def render_transcript(self) -> str:
        width = self.transcript_width()
        with self.lock:
            tab = self.tabs[self.active_tab]
            message_key = self._virtual_message_key(self.messages)
            revision = tab.get("render_revision", 0)
            load_state = (
                bool(tab.get("loading")),
                bool(tab.get("syncing")),
                tab.get("load_error"),
            )
            signature = (
                width,
                self.busy,
                self.busy_tab_index,
                self.active_tab,
                self.show_reasoning,
                tab.get("tab_id"),
                tab.get("conversation_id"),
                message_key,
                revision,
                load_state,
            )
            if signature == getattr(self, "_transcript_signature", None):
                return self._transcript_cache
            messages = [dict(message) for message in self.messages]
        self._transcript_roles = tuple(message.get("role") for message in messages)
        snapshots = getattr(self, "_prepared_transcripts", {})
        tab_id = self.tabs[self.active_tab].get("tab_id")
        snapshot = snapshots.get(tab_id)
        if (
            snapshot
            and snapshot["width"] == width
            and snapshot["labels"] == self.renderer.labels
            and snapshot["reasoning"] == self.show_reasoning
            and snapshot.get("conversation_id") == tab.get("conversation_id")
            and snapshot["message_key"] == message_key
            and snapshot["revision"] == revision
        ):
            snapshots.pop(tab_id)
            snapshots[tab_id] = snapshot
            self._transcript_roles = snapshot["roles"]
            self.message_ranges = snapshot["ranges"]
            self.transcript_line_count = snapshot["text"].count("\n") + 1
            self._ansi_render_key, self._ansi_fragments = snapshot["text"], snapshot["ansi"]
            self._line_render_key, self._transcript_lines = snapshot["text"], snapshot["lines"]
            self._transcript_signature, self._transcript_cache = signature, snapshot["text"]
            return snapshot["text"]
        if self.app.is_running and messages:
            tab = self.tabs[self.active_tab]
            key = tab["tab_id"]
            pending = getattr(self, "_render_pending", set())
            self._render_pending = pending
            if key not in pending:
                pending.add(key)
                labels, reasoning = dict(self.renderer.labels), self.show_reasoning
                conversation_id = tab.get("conversation_id")
                message_key = self._virtual_message_key(tab.get("messages", []))
                revision = tab.get("render_revision", 0)

                def read():
                    return prepare_transcript(
                        messages,
                        width,
                        labels,
                        reasoning,
                        cancelled=lambda: (
                            not any(tab is x for x in self.tabs)
                            or self.tabs[self.active_tab] is not tab
                            or tab.get("conversation_id") != conversation_id
                        ),
                    )

                def ready(result):
                    pending.discard(key)
                    if (
                        tab.get("conversation_id") == conversation_id
                        and self._virtual_message_key(tab.get("messages", [])) == message_key
                        and tab.get("render_revision", 0) == revision
                    ):
                        self.remember_transcript(
                            tab,
                            messages,
                            width,
                            labels,
                            reasoning,
                            *result,
                            message_key=message_key,
                            revision=revision,
                        )
                    self.app.invalidate()

                def failed(reason):
                    pending.discard(key)
                    self.app.invalidate()

                self._read_in_background(read, ready, failed, session_bound=False, cpu=True)
            if (
                snapshot
                and snapshot.get("conversation_id") == tab.get("conversation_id")
                and snapshot["width"] == width
                and snapshot["labels"] == self.renderer.labels
                and snapshot["reasoning"] == self.show_reasoning
            ):
                self._transcript_roles = snapshot["roles"]
                self.message_ranges = snapshot["ranges"]
                self.transcript_line_count = len(snapshot["lines"])
                self._line_render_key, self._transcript_lines = snapshot["text"], snapshot["lines"]
                return snapshot["text"]
            self.message_ranges = []
            self.transcript_line_count = 1
            return self.tr("Sohbet görünümü hazırlanıyor…")
        if not messages:
            self.message_ranges = []
            empty_message = (
                self.tr("Conversation load failed: {0}").format(
                    self.tabs[self.active_tab]["load_error"]
                )
                if self.tabs and self.tabs[self.active_tab].get("load_error")
                else self.tr("Loading conversation…")
                if self.loading_conversation
                or (
                    self.tabs
                    and (
                        self.tabs[self.active_tab].get("loading")
                        or self.tabs[self.active_tab].get("syncing")
                        or self.tabs[self.active_tab].get("history_load_pending")
                    )
                )
                else self.tr("This saved conversation has no messages.")
                if self.tabs and self.tabs[self.active_tab].get("conversation_id")
                else self.tr("Yeni bir sohbet başlatın.")
            )
            buffer = StringIO()
            console = Console(
                file=buffer,
                force_terminal=True,
                color_system="truecolor",
                no_color=False,
                width=width,
                theme=DEFAULT_MARKDOWN_THEME,
            )
            console.print(Align.center(Text(empty_message, style="dim"), width=width))
            rendered = buffer.getvalue()
        else:
            active_index = (
                len(messages) - 1 if self.busy and self.busy_tab_index == self.active_tab else None
            )
            rendered, self.message_ranges = self.renderer.transcript(
                messages, width, self.show_reasoning, active_index=active_index
            )
        self.transcript_line_count = rendered.count("\n") + 1
        self._transcript_signature = signature
        self._transcript_cache = rendered
        return rendered

    def terminal_width(self) -> int:
        try:
            return self.app.output.get_size().columns
        except Exception:
            return shutil.get_terminal_size((100, 24)).columns

    def sidebar_width(self) -> int:
        terminal_width = self.terminal_width()
        preferred = max(22, min(30, terminal_width // 4))
        return min(preferred, max(10, terminal_width - 22))

    def workspace_width(self) -> int:
        return max(
            1, self.terminal_width() - (self.sidebar_width() + 1 if self.sidebar_visible else 0)
        )

    def main_width(self) -> int:
        columns = (
            self.layout_controller.columns()
            if self.layout_controller and self.layout_enabled
            else 1
        )
        return max(8, (self.workspace_width() - columns + 1) // columns)

    def begin_split(self, axis="columns"):
        if self.layout_controller:
            self.layout_controller.begin(axis)
        else:
            self.status = self.tr("Layout eklentisi kullanılamıyor")
            self.app.invalidate()

    def transcript_width(self) -> int:
        return min(
            DEFAULT_MESSAGE_WIDTH,
            max(8, self.main_width() - DEFAULT_TRANSCRIPT_GUTTER_WIDTH - 1),
        )

    def transcript_rows(self, viewport_height=None):
        if self.app.is_running and self.messages:
            return self._virtual_transcript_rows(viewport_height or 1)
        rendered = self.render_transcript()
        reference = getattr(self, "selected_message_ref", None)
        if reference is not None:
            self.selected_message = next(
                (i for i, item in enumerate(self.messages) if item is reference),
                self.selected_message,
            )
        if rendered != getattr(self, "_line_render_key", None):
            if rendered != getattr(self, "_ansi_render_key", None):
                self._ansi_render_key, self._ansi_fragments = (
                    rendered,
                    transcript_fragments(rendered),
                )
            self._transcript_lines = index_transcript(self._ansi_fragments, self.message_ranges)
            self._line_render_key = rendered
        return self._transcript_lines

    @staticmethod
    def _virtual_message_key(messages):
        """Track the transcript's supported mutation paths without scanning it."""
        if not messages:
            return id(messages), 0, None, None, None
        first = messages[0]
        last = messages[-1]
        return id(messages), len(messages), id(first), id(last), id(last.get("text"))

    def _trim_transcript_cache(self):
        """Keep warm tab layouts under one shared budget, evicting inactive LRU tabs."""
        active = self.tabs[self.active_tab]
        cached = [
            (tab.get("render_used", 0), tab, tab["render_layout"])
            for tab in self.tabs
            if tab.get("render_layout") is not None
        ]
        total = sum(layout.resident_bytes for _, _, layout in cached)
        for _, tab, layout in sorted(cached, key=lambda item: item[0]):
            if total <= 4 * 1024 * 1024:
                break
            if tab is not active:
                total -= layout.resident_bytes
                tab.pop("render_layout", None)
                tab.pop("render_signature", None)
                layout.dispose()

    def _virtual_transcript_rows(self, viewport_height):
        """Prepare transcript layout off-thread and retain only viewport pages."""
        from ..transcript import VirtualTranscriptRows

        tab = self.tabs[self.active_tab]
        tab_id = tab.get("tab_id")
        width = self.transcript_width()
        reasoning = self.show_reasoning
        with self.lock:
            messages = self.messages
            message_key = self._virtual_message_key(messages)
            active_index = (
                len(messages) - 1
                if self.busy and self.busy_tab_index == self.active_tab and messages
                else None
            )
        conversation_id = tab.get("conversation_id")
        signature = (
            tab_id,
            conversation_id,
            width,
            reasoning,
            repr(self.renderer.labels),
            message_key,
            active_index,
            tab.get("render_revision", 0),
            bool(tab.get("history_window")),
        )
        layout = getattr(self, "_virtual_transcript", None)
        layout_is_current = (
            layout is not None and getattr(self, "_virtual_signature", None) == signature
        )
        if not layout_is_current:
            if (
                getattr(self, "_virtual_tab_id", None) != tab_id
                or getattr(self, "_virtual_conversation_id", None) != conversation_id
            ):
                owned = any(item.get("render_layout") is layout for item in self.tabs)
                if layout is not None and not owned:
                    layout.dispose()
                self._virtual_transcript = tab.get("render_layout")
                self._virtual_signature = tab.get("render_signature")
                self._virtual_pending_signature = None
                self._transcript_prefetch.clear()
                layout = self._virtual_transcript
                tab["render_used"] = time.monotonic()
            self._virtual_tab_id = tab_id
            self._virtual_conversation_id = conversation_id
            if layout is None or getattr(self, "_virtual_signature", None) != signature:
                self._request_virtual_transcript(
                    tab, width, reasoning, signature, active_index, viewport_height
                )
            layout_is_current = getattr(self, "_virtual_signature", None) == signature
            layout = getattr(self, "_virtual_transcript", None)
        if layout is None:
            self.message_ranges = []
            self.transcript_line_count = 1
            return (([("class:sidebar.muted", self.tr("Sohbet görünümü hazırlanıyor…"))], None),)

        self.message_ranges = layout.ranges
        self.transcript_line_count = layout.line_count
        structure_key = (tab_id, *message_key[:4])
        if structure_key != getattr(self, "_virtual_selection_key", None):
            with self.lock:
                self._transcript_roles = tuple(message.get("role") for message in self.messages)
                reference = getattr(self, "selected_message_ref", None)
                if reference is not None:
                    self.selected_message = next(
                        (i for i, item in enumerate(self.messages) if item is reference),
                        self.selected_message,
                    )
            self._virtual_selection_key = structure_key
        rows = VirtualTranscriptRows(layout)
        last_scroll = max(0, layout.line_count - viewport_height)
        scroll = (
            last_scroll
            if self.follow_output
            else min(max(0, self.transcript_window.vertical_scroll), last_scroll)
        )
        if self.transcript_window.vertical_scroll != scroll:
            self.transcript_window.vertical_scroll = scroll
        if layout_is_current:
            self.history_pager.prefetch(layout, scroll, viewport_height)
        self._transcript_prefetch.request(layout, scroll, viewport_height)
        return rows

    def _request_virtual_transcript(
        self, tab, width, reasoning, signature, active_index, viewport_height
    ):
        if getattr(
            self, "_virtual_failed_signature", None
        ) == signature and time.monotonic() < getattr(self, "_virtual_retry_after", 0):
            return
        pending = getattr(self, "_virtual_pending_signature", None)
        if pending is not None:
            return
        if self.busy:
            elapsed = time.monotonic() - getattr(self, "_virtual_last_request_at", 0)
            delay = 0.08 - elapsed
            if delay > 0:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    pass
                else:
                    if getattr(self, "_virtual_retry_handle", None) is not None:
                        return

                    def retry():
                        self._virtual_retry_handle = None
                        self.app.invalidate()

                    self._virtual_retry_handle = loop.call_later(delay, retry)
                    return
        self._virtual_last_request_at = time.monotonic()
        self._virtual_pending_signature = signature
        conversation_id = tab.get("conversation_id")
        scroll = self.transcript_window.vertical_scroll
        tab_id = tab.get("tab_id")
        manual_revision = getattr(self, "_manual_scroll_revision", 0)
        labels = dict(self.renderer.labels)
        anchor = tab.get("history_anchor")
        previous = getattr(self, "_virtual_transcript", None)
        shared_renderer = (
            previous.renderer
            if previous is not None
            and previous.width == width
            and previous.renderer.labels == labels
            else None
        )

        with self.lock:
            if self._virtual_message_key(self.messages) != signature[5]:
                self._virtual_pending_signature = None
                return
            messages = tuple(
                (message.get("role"), message.get("text", "")) for message in self.messages
            )
            ordinals = tuple(message.get("_archive_ordinal") for message in self.messages)
            message_ids = tuple(message.get("id") for message in self.messages)
        payload = {"snapshot": (messages, ordinals, message_ids)}
        tab["render_payload"] = payload

        def release_payload():
            if tab.get("render_payload") is payload:
                tab.pop("render_payload", None)
            payload.clear()

        def still_active():
            current_active_index = (
                len(self.messages) - 1
                if self.busy and self.busy_tab_index == self.active_tab and self.messages
                else None
            )
            return (
                any(item is tab for item in self.tabs)
                and self.tabs[self.active_tab] is tab
                and self.transcript_width() == width
                and self.show_reasoning == reasoning
                and repr(self.renderer.labels) == signature[4]
                and self._virtual_message_key(self.messages) == signature[5]
                and current_active_index == active_index
                and tab.get("render_revision", 0) == signature[7]
                and bool(tab.get("history_window")) == signature[8]
            )

        def read():
            snapshot = payload.get("snapshot")
            if snapshot is None:
                raise InterruptedError("Transcript closed")
            value = prepare_virtual_transcript(
                snapshot[0],
                width,
                labels,
                reasoning,
                cancelled=lambda: not still_active(),
                active_index=active_index,
                renderer=shared_renderer,
                preserve_order=signature[8],
                previous=previous,
            )
            value.ordinals = snapshot[1]
            value.message_ids = snapshot[2]
            if value.line_count:
                top = self.history_pager.position(
                    value, value.ordinals, anchor, viewport_height, scroll
                )
                value.prefetch(
                    top,
                    min(value.line_count, top + viewport_height),
                    cancelled=lambda: not still_active(),
                )
            return value

        def ready(value):
            release_payload()
            if self._virtual_pending_signature == signature:
                self._virtual_pending_signature = None
            if still_active() and tab.get("tab_id") == tab_id:
                self._virtual_failed_signature = None
                previous = getattr(self, "_virtual_transcript", None)
                previous_owner = next(
                    (item for item in self.tabs if item.get("render_layout") is previous), None
                )
                latest_anchor = anchor
                if anchor is not None and (
                    getattr(self, "_manual_scroll_revision", 0) != manual_revision
                    or (not anchor.get("edge") and self.transcript_window.vertical_scroll != scroll)
                ):
                    latest_anchor = self.history_pager.anchor(tab) or anchor
                if (
                    previous is not None
                    and previous is not value
                    and (previous_owner is None or previous_owner is tab)
                ):
                    previous.dispose(retain_renderer=previous.renderer is value.renderer)
                self._virtual_transcript = value
                if anchor is not None and tab.get("history_anchor") is anchor:
                    self.transcript_window.vertical_scroll = self.history_pager.position(
                        value, value.ordinals, latest_anchor, viewport_height, scroll
                    )
                    tab.pop("history_anchor", None)
                self._virtual_signature = signature
                tab.update(
                    render_layout=value, render_signature=signature, render_used=time.monotonic()
                )
                self._trim_transcript_cache()
                self._virtual_tab_id = tab_id
                self._virtual_conversation_id = conversation_id
                self._transcript_prefetch.clear()
                self.app.invalidate()
            else:
                current = getattr(self, "_virtual_transcript", None)
                value.dispose(
                    retain_renderer=(current is not None and current.renderer is value.renderer)
                    or any(
                        item.get("render_layout") is not None
                        and item["render_layout"].renderer is value.renderer
                        for item in self.tabs
                    )
                )

        def failed(_reason):
            release_payload()
            if self._virtual_pending_signature == signature:
                self._virtual_pending_signature = None
            self._virtual_failed_signature = signature
            self._virtual_retry_after = time.monotonic() + 1
            self.app.invalidate()

        self._read_in_background(read, ready, failed, session_bound=False, cpu=True)

    def open_message_link(self, url, index=None):
        from urllib.parse import unquote

        from ..markdown import open_link

        is_file = url.startswith("siope-file:")
        if is_file:
            url = unquote(url.removeprefix("siope-file:"))

        if self.account_service is None:
            open_link(url)
            return
        key, conversation, title = self.account_key, self.conversation_id, self.title
        message = (
            self.messages[index] if index is not None and 0 <= index < len(self.messages) else {}
        )
        message_id = message.get("id")
        if is_file:
            message_id = next(
                (
                    item.get("message_id") or message_id
                    for item in message.get("_files", ())
                    if item["url"] == url
                ),
                message_id,
            )
        file_name = (
            next((item["name"] for item in message.get("_files", ()) if item["url"] == url), None)
            if is_file
            else None
        )
        self.status = self.tr("Bağlantı hazırlanıyor…")

        def progress(name, size, total):
            label = f"{size // 1024} KiB" + (f" / {total // 1024} KiB" if total else "")

            def report():
                self.status = self.tr("İndiriliyor: {0} · {1}").format(name, label)
                self.app.invalidate()

            self._dispatch_ui(report)

        def completed(result):
            if result["kind"] == "page":
                open_link(result["url"])
                self.status = self.tr("Bağlantı tarayıcıda açıldı")
            else:
                self.status = self.tr("İndirildi: {0}").format(result["path"])
            self.app.invalidate()

        def failed(reason):
            import re

            status = re.fullmatch(r"Dosya alınamadı \(HTTP (\d+)\)", str(reason))
            detail = (
                self.tr("Dosya alınamadı (HTTP {0})").format(status[1])
                if status
                else self.tr(str(reason))
            )
            self.status = self.tr("İndirme başarısız: {0}").format(detail)
            self.app.invalidate()

        self._read_in_background(
            lambda: self.account_service.download(
                key,
                url,
                conversation,
                message_id,
                progress,
                title=title,
                **({"force_download": True, "filename": file_name} if is_file else {}),
            ),
            completed,
            failed,
            session_bound=False,
        )

    def select_message(self, index):
        if index is None or not 0 <= index < len(self.messages):
            return
        self.selected_message = index
        self.selected_message_ref = self.messages[index]
        self.message_selection = False
        self.pause_following()
        self.app.layout.focus(self.transcript_control)
        self.app.invalidate()

    def composer_line_count(self):
        width = max(1, self.main_width() - 4)
        return (
            sum(
                max(1, (get_cwidth(line) + width - 1) // width)
                for line in self.input.text.split("\n")
            )
            if hasattr(self, "input")
            else 1
        )

    def composer_height(self):
        return min(
            DEFAULT_COMPOSER_MAX_LINES,
            self.composer_line_count(),
        )

    def transcript_cursor_position(self) -> Point:
        if self.follow_output:
            return Point(x=0, y=max(0, self.transcript_line_count - 1))
        return Point(x=0, y=max(0, self.transcript_window.vertical_scroll))

    def pause_following(self) -> None:
        self._manual_scroll_revision = getattr(self, "_manual_scroll_revision", 0) + 1
        self.tabs[self.active_tab].pop("history_pending_edge", None)
        self.follow_output = False

    def transcript_page_size(self) -> int:
        info = self.transcript_window.render_info
        if info is None:
            return 10
        return max(3, info.window_height - 3)

    def append_system(self, text: str) -> None:
        with self.lock:
            self.messages.append({"role": "system", "text": text})
        self.scroll_bottom()

    def scroll_bottom(self, force: bool = True) -> None:
        if force:
            self.transcript_window.scroll_direction = 0
        if force and self.history_pager.request("bottom"):
            return
        if force:
            self.follow_output = True
        if self.follow_output:
            self.transcript_window.vertical_scroll = 10**9
        self.app.invalidate()

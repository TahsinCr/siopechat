"""Bounded, row-lazy transcript rendering for the terminal viewport."""

from bisect import bisect_right
from collections import OrderedDict
from threading import RLock

from .markdown import (
    MessageRenderer,
    index_transcript,
    index_transcript_window,
    message_order,
    render_chatgpt_annotations,
    transcript_fragments,
)


class VirtualTranscript:
    """Keep row counts for navigation and a bounded LRU of rendered pages."""

    PAGE_ROWS = 128
    CACHE_BYTES = 1024 * 1024

    def __init__(
        self, messages, width, show_reasoning=True, renderer=None, cancelled=None, active_index=None
    ):
        self.renderer = renderer or MessageRenderer()
        self.width = width
        self.show_reasoning = show_reasoning
        self._lock = RLock()
        self._pages = OrderedDict()
        self._cache_bytes = 0
        self._disposed = False
        self._release_renderer = True
        self.active_index = active_index
        # Retain only the fields needed for drawing.  In particular, do not
        # keep a second copy of every full provider message dictionary.
        self.messages = (
            messages
            if isinstance(messages, tuple) and (not messages or isinstance(messages[0], tuple))
            else tuple(
                message
                if isinstance(message, tuple)
                else (message.get("role"), message.get("text", ""))
                for message in messages
            )
        )
        self.entries = message_order(message[0] for message in self.messages)
        if not show_reasoning:
            self.entries = [
                index for index in self.entries if self.messages[index][0] != "reasoning"
            ]
        self.starts = []
        self.ranges = []
        line = 0
        for index in self.entries:
            if cancelled and cancelled():
                raise InterruptedError("Superseded transcript")
            rendered = self._render(index)
            count = max(1, rendered.count("\n"))
            self.starts.append(line)
            self.ranges.append((line, line + count, index))
            line += count
        self.line_count = line + 1

    def _render(self, index):
        role, text = self.messages[index]
        return self.renderer.render(
            role,
            render_chatgpt_annotations(text or ""),
            self.width,
            index > 0,
            index == self.active_index,
        )

    def _page_key(self, entry_index, page):
        return entry_index, page

    @staticmethod
    def _row_bytes(row):
        return sum(len(span[1].encode("utf-8")) + len(span[0]) * 2 + 48 for span in row[0]) + 64

    def _cache_page(self, entry_index, page, rows, size=None):
        key = self._page_key(entry_index, page)
        previous = self._pages.pop(key, None)
        if previous is not None:
            self._cache_bytes -= previous[1]
        if size is None:
            size = sum(self._row_bytes(row) for row in rows)
        if size > self.CACHE_BYTES:
            return
        self._pages[key] = (rows, size)
        self._cache_bytes += size
        while self._cache_bytes > self.CACHE_BYTES and self._pages:
            _, (_, removed_size) = self._pages.popitem(last=False)
            self._cache_bytes -= removed_size

    def _render_page(self, entry_index, page):
        if self._disposed:
            return ()
        key = self._page_key(entry_index, page)
        cached = self._pages.get(key)
        if cached is not None:
            self._pages.move_to_end(key)
            return cached[0]
        message_index = self.entries[entry_index]
        rendered = self._render(message_index)
        if self._disposed:
            self._release()
            return ()
        fragments = transcript_fragments(rendered)
        count = self.ranges[entry_index][1] - self.ranges[entry_index][0]
        start = page * self.PAGE_ROWS
        if len(rendered) + count * 128 > self.CACHE_BYTES:
            rows = index_transcript_window(
                fragments,
                [(0, count, message_index)],
                start,
                min(count, start + self.PAGE_ROWS),
            )
            self._cache_page(entry_index, page, rows)
            cached = self._pages.get(key)
            return cached[0] if cached is not None else rows

        all_rows = index_transcript(fragments, [(0, count, message_index)])[:count]
        page_sizes = []
        total_size = 0
        for start in range(0, len(all_rows), self.PAGE_ROWS):
            stop = min(len(all_rows), start + self.PAGE_ROWS)
            size = sum(self._row_bytes(all_rows[index]) for index in range(start, stop))
            page_sizes.append(size)
            total_size += size
        if total_size <= self.CACHE_BYTES:
            for page_number, start in enumerate(range(0, len(all_rows), self.PAGE_ROWS)):
                rows = tuple(all_rows[start : start + self.PAGE_ROWS])
                size = page_sizes[page_number]
                self._cache_page(entry_index, page_number, rows, size)
        else:
            start = page * self.PAGE_ROWS
            rows = tuple(all_rows[start : start + self.PAGE_ROWS])
            if page < len(page_sizes):
                self._cache_page(entry_index, page, rows, page_sizes[page])
        cached = self._pages.get(key)
        if cached is not None:
            return cached[0]
        return tuple(all_rows[start : start + self.PAGE_ROWS])

    def _entry_for_line(self, line):
        if line < 0 or line >= self.line_count:
            raise IndexError(line)
        if line == self.line_count - 1:
            return None, 0
        entry = bisect_right(self.starts, line) - 1
        return entry, line - self.starts[entry]

    def row(self, line):
        """Blocking access used by explicit text selection and mouse actions."""
        if self._disposed:
            return (), None
        entry, local = self._entry_for_line(line)
        if entry is None:
            return (), None
        with self._lock:
            if self._disposed:
                return (), None
            page = local // self.PAGE_ROWS
            rows = self._render_page(entry, page)
            if self._disposed:
                self._release()
                return (), None
            offset = local % self.PAGE_ROWS
            return rows[offset] if offset < len(rows) else ((), self.entries[entry])

    def cached_row(self, line):
        """Non-blocking paint access; a miss is filled by viewport prefetch."""
        if self._disposed:
            return None
        entry, local = self._entry_for_line(line)
        if entry is None:
            return (), None
        key = self._page_key(entry, local // self.PAGE_ROWS)
        if not self._lock.acquire(blocking=False):
            return None
        try:
            if self._disposed:
                return None
            cached = self._pages.get(key)
            if cached is None:
                return None
            self._pages.move_to_end(key)
            rows = cached[0]
            offset = local % self.PAGE_ROWS
            return rows[offset] if offset < len(rows) else ((), self.entries[entry])
        finally:
            self._lock.release()

    def prefetch(self, start, stop, cancelled=None):
        """Render pages intersecting a viewport plus its caller-selected overscan."""
        if self._disposed:
            return
        start = min(max(0, start), max(0, self.line_count - 1))
        stop = min(self.line_count, max(start, stop))
        if stop <= start:
            return
        final_message_line = self.line_count - 2
        if start > final_message_line:
            return
        first_line = min(start, final_message_line)
        last_line = min(stop - 1, final_message_line)
        first, last = self._entry_for_line(first_line)[0], self._entry_for_line(last_line)[0]
        if first is None or last is None:
            return
        for entry in range(first, last + 1):
            if self._disposed or (cancelled and cancelled()):
                return
            entry_start, entry_end, _ = self.ranges[entry]
            local_start = max(start, entry_start) - entry_start
            local_end = min(stop, entry_end) - entry_start
            if local_end <= local_start:
                continue
            first_page = local_start // self.PAGE_ROWS
            last_page = (local_end - 1) // self.PAGE_ROWS
            for page in range(first_page, last_page + 1):
                if self._disposed or (cancelled and cancelled()):
                    return
                with self._lock:
                    try:
                        self._render_page(entry, page)
                    finally:
                        if self._disposed:
                            self._release()

    @property
    def cache_bytes(self):
        return self._cache_bytes

    def clear(self):
        with self._lock:
            self._pages.clear()
            self._cache_bytes = 0

    def _release(self):
        self._pages.clear()
        self._cache_bytes = 0
        self.messages = ()
        if self._release_renderer:
            self.renderer.clear_cache(wait=False)

    def dispose(self, *, retain_renderer=False):
        """Never wait on the UI thread for an in-flight Markdown conversion."""
        self._disposed = True
        self._release_renderer = not retain_renderer
        if self._lock.acquire(blocking=False):
            try:
                self._release()
            finally:
                self._lock.release()


class VirtualTranscriptRows:
    """Sequence-compatible row view consumed by TranscriptControl."""

    def __init__(self, transcript):
        self.transcript = transcript

    def __len__(self):
        return self.transcript.line_count

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self.transcript.row(i) for i in range(*index.indices(len(self))))
        if index < 0:
            index += len(self)
        return self.transcript.row(index)

    def paint_row(self, index):
        return self.transcript.cached_row(index)

    def prefetch(self, start, stop, cancelled=None):
        return self.transcript.prefetch(start, stop, cancelled)

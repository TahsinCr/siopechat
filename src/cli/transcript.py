"""Bounded, row-lazy transcript rendering for the terminal viewport."""

from bisect import bisect_right
from collections import OrderedDict
from threading import RLock

from .markdown import (
    MessageRenderer,
    index_transcript,
    render_chatgpt_annotations,
    transcript_fragments,
)


class VirtualTranscript:
    """Keep row counts for navigation and only cache rendered viewport pages."""

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
        self._cancelled = cancelled
        self.active_index = active_index
        self.entries = self._ordered(messages)
        if not show_reasoning:
            self.entries = [entry for entry in self.entries if entry[1].get("role") != "reasoning"]
        self.starts = []
        self.ranges = []
        line = 0
        for index, message in self.entries:
            if cancelled and cancelled():
                raise InterruptedError("Superseded transcript")
            rendered = self._render(index, message)
            count = max(1, rendered.count("\n"))
            self.starts.append(line)
            self.ranges.append((line, line + count, index))
            line += count
        self.line_count = line + 1

    @staticmethod
    def _ordered(messages):
        ordered, pending = [], []
        for entry in enumerate(messages):
            if entry[1].get("role") in ("user", "system"):
                ordered.extend(sorted(pending, key=lambda item: item[1].get("role") != "reasoning"))
                pending = []
                ordered.append(entry)
            else:
                pending.append(entry)
        ordered.extend(sorted(pending, key=lambda item: item[1].get("role") != "reasoning"))
        return ordered

    def _render(self, index, message):
        return self.renderer.render(
            message["role"],
            render_chatgpt_annotations(message.get("text", "")),
            self.width,
            index > 0,
            index == self.active_index,
        )

    def _page_key(self, entry_index, page):
        return entry_index, page

    @staticmethod
    def _row_bytes(row):
        return sum(len(span[1].encode("utf-8")) + len(span[0]) * 2 + 48 for span in row[0]) + 64

    def _cache_page(self, entry_index, page, rows):
        key = self._page_key(entry_index, page)
        previous = self._pages.pop(key, None)
        if previous is not None:
            self._cache_bytes -= previous[1]
        size = sum(self._row_bytes(row) for row in rows)
        if size > self.CACHE_BYTES:
            return
        self._pages[key] = (rows, size)
        self._cache_bytes += size
        while self._cache_bytes > self.CACHE_BYTES and self._pages:
            _, (_, removed_size) = self._pages.popitem(last=False)
            self._cache_bytes -= removed_size

    def _render_page(self, entry_index, page):
        key = self._page_key(entry_index, page)
        cached = self._pages.get(key)
        if cached is not None:
            self._pages.move_to_end(key)
            return cached[0]
        message_index, message = self.entries[entry_index]
        rendered = self._render(message_index, message)
        fragments = transcript_fragments(rendered)
        count = self.ranges[entry_index][1] - self.ranges[entry_index][0]
        all_rows = index_transcript(fragments, [(0, count, message_index)])[:count]
        start = page * self.PAGE_ROWS
        rows = tuple(all_rows[start : start + self.PAGE_ROWS])
        self._cache_page(entry_index, page, rows)
        return rows

    def _entry_for_line(self, line):
        if line < 0 or line >= self.line_count:
            raise IndexError(line)
        if line == self.line_count - 1:
            return None, 0
        entry = bisect_right(self.starts, line) - 1
        return entry, line - self.starts[entry]

    def row(self, line):
        """Blocking access used by explicit text selection and mouse actions."""
        entry, local = self._entry_for_line(line)
        if entry is None:
            return (), None
        with self._lock:
            page = local // self.PAGE_ROWS
            rows = self._render_page(entry, page)
            offset = local % self.PAGE_ROWS
            return rows[offset] if offset < len(rows) else ((), self.entries[entry][0])

    def cached_row(self, line):
        """Non-blocking paint access; a miss is filled by viewport prefetch."""
        entry, local = self._entry_for_line(line)
        if entry is None:
            return (), None
        key = self._page_key(entry, local // self.PAGE_ROWS)
        if not self._lock.acquire(blocking=False):
            return None
        try:
            cached = self._pages.get(key)
            if cached is None:
                return None
            self._pages.move_to_end(key)
            rows = cached[0]
            offset = local % self.PAGE_ROWS
            return rows[offset] if offset < len(rows) else ((), self.entries[entry][0])
        finally:
            self._lock.release()

    def prefetch(self, start, stop, cancelled=None):
        """Render pages intersecting a viewport plus its caller-selected overscan."""
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
        keep = set()
        for entry in range(first, last + 1):
            if cancelled and cancelled():
                return
            entry_start, entry_end, _ = self.ranges[entry]
            local_start = max(start, entry_start) - entry_start
            local_end = min(stop, entry_end) - entry_start
            if local_end <= local_start:
                continue
            first_page = local_start // self.PAGE_ROWS
            last_page = (local_end - 1) // self.PAGE_ROWS
            for page in range(first_page, last_page + 1):
                if cancelled and cancelled():
                    return
                keep.add(self._page_key(entry, page))
                with self._lock:
                    self._render_page(entry, page)
        with self._lock:
            for key in tuple(self._pages):
                if key not in keep:
                    _, size = self._pages.pop(key)
                    self._cache_bytes -= size

    @property
    def cache_bytes(self):
        return self._cache_bytes

    def clear(self):
        with self._lock:
            self._pages.clear()
            self._cache_bytes = 0


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

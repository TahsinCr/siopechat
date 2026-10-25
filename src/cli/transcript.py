"""Bounded, row-lazy transcript rendering for the terminal viewport."""

from bisect import bisect_right
from collections import OrderedDict
from copy import copy
from threading import RLock

from .markdown import (
    MessageRenderer,
    group_reasoning_entries,
    index_transcript,
    index_transcript_window,
    join_reasoning_texts,
    message_order,
    render_chatgpt_annotations,
    transcript_fragments,
)


class VirtualTranscript:
    """Keep row counts for navigation and a bounded LRU of rendered pages."""

    PAGE_ROWS = 128
    CACHE_BYTES = 1024 * 1024

    def __init__(
        self,
        messages,
        width,
        show_reasoning=True,
        renderer=None,
        cancelled=None,
        active_index=None,
        preserve_order=False,
        previous=None,
    ):
        self.renderer = renderer or MessageRenderer()
        self.width = width
        self.show_reasoning = show_reasoning
        self._lock = RLock()
        self._pages = OrderedDict()
        self._paint_pages = {}
        self._cache_bytes = 0
        self._disposed = False
        self._release_renderer = True
        self.active_index = active_index
        self._large_code = {}
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
        self.entries = (
            list(range(len(self.messages)))
            if preserve_order
            else message_order(message[0] for message in self.messages)
        )
        if not show_reasoning:
            self.entries = [
                index for index in self.entries if self.messages[index][0] != "reasoning"
            ]
        self.groups = group_reasoning_entries(
            self.entries, tuple(message[0] for message in self.messages)
        )
        self.entries = [indices[0] for _, indices in self.groups]
        self.starts = []
        self.ranges = []
        self.body_counts = []
        reusable = {}
        previous_blocks = {}
        if (
            previous is not None
            and not previous._disposed
            and previous.width == width
            and previous.renderer.labels == self.renderer.labels
        ):
            with previous._lock:
                previous_messages = previous.messages
                previous_blocks = dict(previous._large_code)
                if previous_messages:
                    for entry in range(len(previous.ranges)):
                        reusable[previous._entry_key(entry)] = (
                            entry,
                            previous.entries[entry],
                            previous.body_counts[entry],
                        )
        inherited = {}
        line = 0
        for entry_index, (role, indices) in enumerate(self.groups):
            if cancelled and cancelled():
                raise InterruptedError("Superseded transcript")
            index = indices[0]
            text = self._entry_text(entry_index)
            active = active_index in indices
            old = reusable.get(self._entry_key(entry_index))
            previous_active = (
                previous.active_index in previous.groups[old[0]][1] if old is not None else False
            )
            if old is not None and active == previous_active:
                old_entry, _, count = old
                inherited[old_entry] = (entry_index, index)
                if old_entry in previous_blocks:
                    block, prefix = previous_blocks[old_entry]
                    if block.source != text:
                        block = copy(block)
                        block.source = text
                    self._large_code[entry_index] = (block, prefix)
            elif (
                block := self.renderer.large_code_block(text or "", self.width, self.PAGE_ROWS)
            ) is None:
                count = self.renderer.line_count(
                    role,
                    text or "",
                    self.width,
                    entry_index > 0,
                    active,
                )
            else:
                prefix = self.renderer.code_prefix_rows(role, self.width)
                self._large_code[entry_index] = (block, prefix)
                count = prefix + block.line_count + 1
            self.body_counts.append(count)
            count += int(role in ("user", "assistant"))
            self.starts.append(line)
            self.ranges.append((line, line + count, index))
            line += count
        self.line_count = line + 1
        if inherited:
            with previous._lock:
                for (entry, page), (rows, size) in previous._pages.items():
                    if entry in inherited:
                        new_entry, owner = inherited[entry]
                        self._cache_page(
                            new_entry, page, tuple((spans, owner) for spans, _ in rows), size
                        )

    def _entry_key(self, entry_index):
        role, indices = self.groups[entry_index]
        return role, tuple(self.messages[index][1] or "" for index in indices)

    def _entry_text(self, entry_index):
        _, texts = self._entry_key(entry_index)
        return texts[0] if len(texts) == 1 else join_reasoning_texts(texts)

    def _render(self, entry_index):
        role, _ = self.groups[entry_index]
        text = self._entry_text(entry_index)
        return self.renderer.render(
            role,
            render_chatgpt_annotations(text or ""),
            self.width,
            entry_index > 0,
            self.active_index in self.groups[entry_index][1],
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
            self._paint_pages = dict(self._pages)
            return
        self._pages[key] = (rows, size)
        self._cache_bytes += size
        while self._cache_bytes > self.CACHE_BYTES and self._pages:
            _, (_, removed_size) = self._pages.popitem(last=False)
            self._cache_bytes -= removed_size
        # Publish immutable row references, not a second rendered copy. Painting
        # can keep using ready rows while another page holds the render lock.
        self._paint_pages = dict(self._pages)

    def _render_page(self, entry_index, page):
        if self._disposed:
            return ()
        key = self._page_key(entry_index, page)
        cached = self._pages.get(key)
        if cached is not None:
            self._pages.move_to_end(key)
            return cached[0]
        message_index = self.entries[entry_index]
        count = self.body_counts[entry_index]
        start = page * self.PAGE_ROWS
        large_code = self._large_code.get(entry_index)
        if large_code is not None:
            block, prefix = large_code
            stop = min(count, start + self.PAGE_ROWS)
            code_start = max(0, start - prefix)
            code_stop = min(block.line_count, max(0, stop - prefix))
            rendered = self.renderer.render_large_code_window(
                self.messages[message_index][0],
                block,
                code_start,
                code_stop,
                include_label=start == 0,
            )
            suffix_start = prefix + block.line_count
            suffix_rows = max(0, min(stop, suffix_start + 1) - max(start, suffix_start))
            if suffix_rows:
                rendered += "\n" * suffix_rows
            rows = index_transcript(
                transcript_fragments(rendered), [(0, stop - start, message_index)]
            )[: stop - start]
            self._cache_page(entry_index, page, rows)
            cached = self._pages.get(key)
            return cached[0] if cached is not None else rows

        rendered = self._render(entry_index)
        if self._disposed:
            self._release()
            return ()
        fragments = transcript_fragments(rendered)
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
        if local >= self.body_counts[entry] - 1:
            return (), self.entries[entry]
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
        if local >= self.body_counts[entry] - 1:
            return (), self.entries[entry]
        key = self._page_key(entry, local // self.PAGE_ROWS)
        cached = self._paint_pages.get(key)
        if cached is None or self._disposed:
            return None
        rows = cached[0]
        offset = local % self.PAGE_ROWS
        return rows[offset] if offset < len(rows) else ((), self.entries[entry])

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
            local_end = min(local_end, max(1, self.body_counts[entry] - 1))
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

    @property
    def resident_bytes(self):
        """Budget render payloads and indexes; source strings belong to the tab."""
        indexes = sum(
            len(block.offsets) * block.offsets.itemsize + len(block.parts) * block.parts.itemsize
            for block, _ in self._large_code.values()
        )
        return self._cache_bytes + self.renderer.cache_bytes + indexes + 256 * len(self.messages)

    def clear(self):
        with self._lock:
            self._pages.clear()
            self._paint_pages = {}
            self._cache_bytes = 0

    def _release(self):
        self._pages.clear()
        self._paint_pages = {}
        self._cache_bytes = 0
        self._large_code.clear()
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


class TranscriptPrefetch:
    """One render job and one latest viewport, shared by main and split panes."""

    def __init__(self, submit, invalidate):
        self.submit, self.invalidate = submit, invalidate
        self._target = self._done = self._running = None
        self._generation = 0

    def clear(self):
        self._generation += 1
        self._target = self._done = None

    def request(self, layout, top, height):
        height = max(1, height)
        top = min(max(0, top), max(0, layout.line_count - height))
        key = (id(layout), top, height)
        self._target = (key, layout)
        if self._running is not None or self._done == key:
            return
        token, generation = object(), self._generation
        self._running = token

        def read():
            target = self._target
            if target is None or generation != self._generation:
                return None
            current, source = target
            _, start, size = current

            def cancelled():
                return (
                    generation != self._generation
                    or self._target is None
                    or self._target[0] != current
                )

            source.prefetch(start, min(source.line_count, start + size), cancelled)
            if not cancelled() and self._target == target:
                source.prefetch(
                    max(0, start - 2 * size),
                    min(source.line_count, start + 3 * size),
                    lambda: cancelled() or self._target != target,
                )
            if not cancelled():
                source.prefetch(start, min(source.line_count, start + size), cancelled)
            return current if not cancelled() else None

        def finished(result=None):
            if self._running is token:
                self._running = None
                self._done = result if generation == self._generation else None
                self.invalidate()

        self.submit(read, finished, lambda _reason: finished())

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

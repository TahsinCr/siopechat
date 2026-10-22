"""Rich Markdown rendering and provider annotation presentation."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from array import array
from collections import OrderedDict
from io import StringIO
from threading import RLock
from typing import Any
from urllib.parse import urlsplit

from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.mouse_events import MouseButton, MouseEventType
from rich.cells import cell_len
from rich.console import Console
from rich.markdown import BlockQuote, CodeBlock, Markdown
from rich.padding import Padding
from rich.segment import Segment
from rich.style import Style as RichStyle
from rich.syntax import Syntax
from rich.text import Text
from rich.theme import Theme

from .styles import DEFAULT_MARKDOWN_STYLE

ANNOTATION_PATTERN = re.compile(r"\ue200([^\ue201\ue202]+)(?:\ue202(.*?))?\ue201")
FILE_CITATION_PATTERN = re.compile(r"[ \t]*\[F\d+:L\d+(?:[ \t]*[-–—][ \t]*L?\d+)?\]")
WRITING_DIRECTIVE_PATTERN = re.compile(
    r"(?m)^[\ue200]?\s*:::writing\{(?P<attributes>[^}\n]*)\}\s*$"
)

MARKDOWN_THEME = Theme(DEFAULT_MARKDOWN_STYLE)
MAX_CODE_HIGHLIGHT_CHARS = 64 * 1024


def detect_code_language(code: str) -> str:
    """Choose a useful lexer for common unlabelled Markdown code blocks."""
    stripped = code.lstrip()
    if re.search(r"^(?:from\s+\w+\s+import|import\s+\w+|def\s+\w+|class\s+\w+)", stripped, re.M):
        return "python"
    if re.search(
        r"^(?:use\s+(?:std|crate)::|(?:pub\s+)?fn\s+\w+|impl(?:<.*?>)?\s+\w+)", stripped, re.M
    ):
        return "rust"
    if re.search(r"\b(?:const|let|var)\s+\w+\s*=|=>|console\.\w+\(", code):
        return "javascript"
    if re.search(r"^\s*(?:SELECT|INSERT|UPDATE|DELETE|CREATE TABLE)\b", code, re.I | re.M):
        return "sql"
    if stripped.startswith(("#!/bin/sh", "#!/usr/bin/env bash", "#!/bin/bash")):
        return "bash"
    if stripped.startswith(("{", "[")):
        try:
            import json

            json.loads(stripped)
            return "json"
        except ValueError:
            pass
    if re.search(r"</?[A-Za-z][^>]*>", code):
        return "html"
    if re.search(r"^\s*#include\s*[<\"]|\b(?:int|void|char)\s+\w+\s*\([^)]*\)\s*\{", code, re.M):
        return "c"
    return "text"


class HighlightedCodeBlock(CodeBlock):
    def __rich_console__(self, console: Console, options: Any) -> Any:
        code = str(self.text).rstrip()
        if len(code) > MAX_CODE_HIGHLIGHT_CHARS:
            # Pygments keeps token/style objects for the complete block. Large
            # provider outputs should remain readable without multiplying their
            # peak memory and render time by the source length.
            yield Text(code, style="markdown.code", no_wrap=False, overflow="fold")
            return
        lexer = self.lexer_name
        if lexer == "text":
            lexer = detect_code_language(code)
        yield Syntax(code, lexer, theme=self.theme, word_wrap=True, padding=(0, 1))


def _large_fenced_code_range(text: str) -> tuple[int, int] | None:
    """Find a lone fenced block's body without copying it or building Markdown tokens."""
    opening_end = text.find("\n")
    if opening_end <= 0:
        return None
    opening_line = text[:opening_end].rstrip("\r")
    opening = opening_line.lstrip(" ")
    if len(opening_line) - len(opening) > 3:
        return None
    fence_size = len(opening) - len(opening.lstrip("`"))
    if fence_size < 3 or "`" in opening[fence_size:]:
        return None

    end = len(text)
    while end and text[end - 1] in " \t\r\n":
        end -= 1
    closing_start = text.rfind("\n", 0, end) + 1
    if closing_start <= opening_end:
        return None
    closing_line = text[closing_start:end]
    closing = closing_line.lstrip(" ")
    if len(closing_line) - len(closing) > 3:
        return None
    closing_size = len(closing) - len(closing.lstrip("`"))
    if closing_size < fence_size or closing[closing_size:].strip():
        return None

    body_start = opening_end + 1
    body_end = closing_start
    if body_end > body_start and text[body_end - 1] == "\n":
        body_end -= 1
        if body_end > body_start and text[body_end - 1] == "\r":
            body_end -= 1
    return body_start, body_end


def _single_large_fenced_code(text: str) -> str | None:
    """Return a lone large fenced block's body without building Markdown tokens."""
    bounds = _large_fenced_code_range(text)
    return text[bounds[0] : bounds[1]] if bounds is not None else None


class LargeCodeBlock:
    """Index and render windows of a large, plain fenced code block."""

    def __init__(self, source: str, bounds: tuple[int, int], width: int, page_rows: int):
        self.source = source
        self.start, self.end = bounds
        self.outer_width = max(1, width)
        self.width = max(1, width - 2)
        self.page_rows = page_rows
        self._wrap_console = Console(width=self.width, force_terminal=False)
        self.offsets = array("Q", [0])
        self.parts = array("I", [0])
        self.line_count = 0
        self._index_lines()

    def _wrapped(self, line: str):
        return Text(line, style="markdown.code", no_wrap=False, overflow="fold").wrap(
            self._wrap_console, self.width, overflow="fold"
        )

    def _index_lines(self):
        offset = self.start
        next_checkpoint = self.page_rows
        while offset < self.end:
            line_start = offset
            newline = self.source.find("\n", offset, self.end)
            if newline < 0:
                line_end = self.end
                offset = self.end
            else:
                line_end = newline
                if line_end > line_start and self.source[line_end - 1] == "\r":
                    line_end -= 1
                offset = newline + 1
            line = self.source[line_start:line_end]
            if "\t" not in line and cell_len(line) <= self.width:
                row_count = 1
            else:
                row_count = len(self._wrapped(line))
            line_end = self.line_count + row_count
            while next_checkpoint <= line_end:
                if next_checkpoint == line_end:
                    self.offsets.append(offset)
                    self.parts.append(0)
                else:
                    self.offsets.append(line_start)
                    self.parts.append(next_checkpoint - self.line_count)
                next_checkpoint += self.page_rows
            self.line_count = line_end

        if self.end > self.start and self.source[self.end - 1] in "\n\r":
            # Rich renders the explicit final newline as an empty visual row.
            self.line_count += 1

    def rows(self, start: int, stop: int):
        """Render only visual rows in [start, stop)."""
        if start < 0 or stop <= start or start >= self.line_count:
            return ()
        checkpoint = start // self.page_rows
        source_offset = self.offsets[checkpoint]
        skip = self.parts[checkpoint]
        visual_row = checkpoint * self.page_rows
        source_offset = max(source_offset, self.start)
        rows = []
        while visual_row < stop and source_offset < self.end:
            newline = self.source.find("\n", source_offset, self.end)
            if newline < 0:
                line_end = self.end
                next_offset = self.end
            else:
                line_end = newline
                if line_end > source_offset and self.source[line_end - 1] == "\r":
                    line_end -= 1
                next_offset = newline + 1
            line = self.source[source_offset:line_end]
            if "\t" not in line and cell_len(line) <= self.width:
                wrapped = (Text(line, style="markdown.code"),)
            else:
                wrapped = self._wrapped(line)
            for part in wrapped[skip:]:
                if visual_row >= start:
                    rows.append(part)
                visual_row += 1
                if visual_row >= stop:
                    break
            skip = 0
            source_offset = next_offset
        if (
            len(rows) < stop - start
            and self.end > self.start
            and self.source[self.end - 1] in "\n\r"
        ):
            rows.append(Text("", style="markdown.code"))
        return tuple(rows[: stop - start])


class CompactBlockQuote(BlockQuote):
    """Render quotes as a narrow rail instead of a full-width block."""

    def __rich_console__(self, console: Console, options: Any) -> Any:
        quote_options = options.update(width=max(1, options.max_width - 4))
        lines = console.render_lines(self.elements, quote_options, style=self.style)
        rail = Segment("│ ", RichStyle(color="#6b7280", dim=True))
        for line in lines:
            yield rail
            yield from line
            yield Segment("\n")


class ChatMarkdown(Markdown):
    elements = {
        **Markdown.elements,
        "blockquote_open": CompactBlockQuote,
        "fence": HighlightedCodeBlock,
        "code_block": HighlightedCodeBlock,
    }

    def __init__(self, markup, **kwargs):
        super().__init__(markup, **kwargs)
        # Linkify prose only: code blocks and inline code retain their original text.
        pattern = re.compile(r"(?:https?://|sandbox:/|computer:///)[^\s<>\"]+")
        for block in self.parsed:
            if not block.children:
                continue
            children, depth = [], 0
            for token in block.children:
                if token.type == "link_open":
                    depth += 1
                elif token.type == "link_close":
                    depth -= 1
                if token.type != "text" or depth:
                    children.append(token)
                    continue
                position = 0
                for match in pattern.finditer(token.content):
                    url = match.group().rstrip(".,;!")
                    while url.endswith(")") and url.count(")") > url.count("("):
                        url = url[:-1]
                    if match.start() > position:
                        text = type(token)("text", "", 0)
                        text.content = token.content[position : match.start()]
                        children.append(text)
                    opening = type(token)("link_open", "a", 1)
                    opening.attrSet("href", url)
                    text = type(token)("text", "", 0)
                    text.content = url
                    children.extend((opening, text, type(token)("link_close", "a", -1)))
                    position = match.start() + len(url)
                if position:
                    if position < len(token.content):
                        text = type(token)("text", "", 0)
                        text.content = token.content[position:]
                        children.append(text)
                else:
                    children.append(token)
            block.children = children


def render_chatgpt_annotations(text: str) -> str:
    """Turn ChatGPT web's private-use annotations into terminal-safe text."""

    hidden_citation = "\x00"

    writing_directive = WRITING_DIRECTIVE_PATTERN.search(text)
    if writing_directive:
        attributes = writing_directive.group("attributes")
        title_match = re.search(r'\btitle="([^"]+)"', attributes)
        title = title_match.group(1) if title_match else "Document"
        text = WRITING_DIRECTIVE_PATTERN.sub(f"**{title}**", text, count=1)
        # The web client treats the matching container delimiter as metadata.
        text = re.sub(r"(?m)^\s*:::\s*$", "", text)

    def replace(match: re.Match[str]) -> str:
        kind = match.group(1).casefold()
        if kind == "filecite":
            return hidden_citation
        if kind == "cite":
            return hidden_citation
        # Image groups, navigation lists, entities, and other web-only widgets
        # have no useful terminal payload in the marker itself.
        return ""

    rendered = ANNOTATION_PATTERN.sub(replace, text)

    def remove_hidden_citation(match: re.Match[str]) -> str:
        left = rendered[match.start() - 1] if match.start() else "\n"
        right = rendered[match.end()] if match.end() < len(rendered) else "\n"
        return " " if left != "\n" and right != "\n" else ""

    rendered = re.sub(
        rf"[ \t]*{re.escape(hidden_citation)}[ \t]*",
        remove_hidden_citation,
        rendered,
    )
    # File citations are useful only when the client can resolve their hidden
    # source metadata. Keep them in stored messages but omit them from display.
    rendered = FILE_CITATION_PATTERN.sub("", rendered)
    # Some ChatGPT Web conversation nodes escape every Markdown delimiter in
    # otherwise complete Markdown. Only unescape after detecting a fenced block
    # so intentional backslashes in ordinary prose and source code survive.
    if re.search(r"(?m)^\s*\\`\\`\\`", rendered):
        rendered = re.sub(r"\\([`*_{}\[\]()#+.!>|~-])", r"\1", rendered)
    # Streaming can end between the opening marker and its terminator. Hide the
    # incomplete suffix until the next delta completes it.
    rendered = re.sub(r"\ue200[^\ue201]*$", "", rendered)
    rendered = rendered.replace("\ue200", "").replace("\ue201", "").replace("\ue202", "")
    rendered = re.sub(r"[ \t]+([,.;:!?])", r"\1", rendered)
    return rendered


def message_order(roles):
    """Keep published reasoning before the answer of its own user turn."""
    roles = tuple(roles)
    ordered, pending = [], []
    for index, role in enumerate(roles):
        if role in ("user", "system"):
            ordered.extend(sorted(pending, key=lambda item: roles[item] != "reasoning"))
            pending = []
            ordered.append(index)
        else:
            pending.append(index)
    ordered.extend(sorted(pending, key=lambda item: roles[item] != "reasoning"))
    return ordered


def visible_chat_message(message: dict[str, Any]) -> dict[str, str] | None:
    """Return user-facing turns while excluding web tool protocol nodes."""
    role = message.get("role")
    text = message.get("text")
    content_type = message.get("content_type")
    if message.get("hidden"):
        return None
    reasoning = role == "reasoning" or (
        role == "assistant"
        and (
            message.get("channel") == "analysis"
            or content_type in ("thoughts", "reasoning_recap", "reasoning_summary")
        )
    )
    if reasoning:
        visible = message.get("recipient") in (None, "all")
    elif role == "user":
        visible = content_type in (None, "text")
    elif role == "assistant":
        visible = (
            content_type in (None, "text")
            and message.get("channel") in (None, "commentary", "final")
            and message.get("recipient") in (None, "all")
        )
    else:
        visible = False
    if not visible or not isinstance(text, str) or not text:
        return None
    return {"role": "reasoning" if reasoning else role, "text": text} | {
        key: message[key]
        for key in ("id", "local_pending", "_archive_ordinal")
        if key in message and message[key] is not None
    }


OSC_LINK = re.compile(r"\x1b\]8;[^;]*;([^\x1b\x07]*)(?:\x1b\\|\x07)")


def open_link(url):
    """Open web links without a shell or child output touching the terminal UI."""
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        return
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("https", "http") or not parsed.hostname:
            return
    except ValueError:
        return
    try:
        if sys.platform == "win32":
            os.startfile(url)
            return
        if sys.platform == "darwin":
            command = ["/usr/bin/open", url]
        elif shutil.which("xdg-open"):
            command = ["xdg-open", url]
        elif shutil.which("gio"):
            command = ["gio", "open", url]
        else:
            command = [sys.executable, "-m", "webbrowser", "-t", url]
        # BROWSER may point to a removed browser and override the desktop default.
        environment = dict(os.environ)
        environment.pop("BROWSER", None)
        subprocess.Popen(
            command,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        # An unavailable browser launcher must not crash the conversation view.
        return


def link_handler(url):
    def click(event):
        if event.event_type == MouseEventType.MOUSE_UP and event.button == MouseButton.LEFT:
            open_link(url)
            return None
        return NotImplemented

    click.url = url
    return click


def transcript_fragments(text):
    """Preserve Rich OSC-8 link targets through Prompt Toolkit's ANSI conversion."""
    if "\x1b]8;" not in text:
        return ANSI(text).__pt_formatted_text__()
    chunks, ranges = [], []
    offset, cursor, target = 0, 0, None
    for match in OSC_LINK.finditer(text):
        chunk = text[cursor : match.start()]
        chunks.append(chunk)
        length = sum(len(part[1]) for part in ANSI(chunk).__pt_formatted_text__())
        if target and length:
            ranges.append((offset, offset + length, target))
        offset += length
        target = match.group(1) or None
        cursor = match.end()
    tail = text[cursor:]
    chunks.append(tail)
    if target:
        length = sum(len(part[1]) for part in ANSI(tail).__pt_formatted_text__())
        ranges.append((offset, offset + length, target))
    fragments, offset, index = [], 0, 0
    handlers = {}
    for style, value in ANSI("".join(chunks)).__pt_formatted_text__():
        while value:
            while index < len(ranges) and offset >= ranges[index][1]:
                index += 1
            span = ranges[index] if index < len(ranges) else None
            linked = span is not None and offset >= span[0]
            stop = span[1] if linked else span[0] if span else offset + len(value)
            size = min(len(value), stop - offset)
            piece, value = value[:size], value[size:]
            if linked:
                url = span[2]
                if url not in handlers:
                    handlers[url] = link_handler(url)
                handler = handlers[url]
                fragments.append((style, piece, handler))
            else:
                fragments.append((style, piece))
            offset += size
    return fragments


def index_transcript(ansi, ranges):
    """Coalesce styled spans once and index logical rows for lazy terminal paint."""
    rows = [[]]
    for fragment in ansi:
        style, text = fragment[:2]
        handler = fragment[2] if len(fragment) > 2 else None
        for part in text.splitlines(keepends=True):
            newline = part.endswith("\n")
            value = part[:-1] if newline else part
            row = rows[-1]
            if value:
                if row and row[-1][0] == style and row[-1][2] is handler:
                    row[-1][1].append(value)
                else:
                    row.append((style, [value], handler))
            if newline:
                rows.append([])
    indexed, position = [], 0
    for number, spans in enumerate(rows):
        while position < len(ranges) and number >= ranges[position][1]:
            position += 1
        message = ranges[position][2] if position < len(ranges) else None
        indexed.append(
            (
                tuple(
                    (style, "".join(parts), handler) if handler else (style, "".join(parts))
                    for style, parts, handler in spans
                ),
                message,
            )
        )
    return tuple(indexed)


def index_transcript_window(ansi, ranges, start, stop):
    """Index only a requested row window without building every row in memory."""
    if not ranges or stop <= start:
        return ()
    start = max(0, start)
    stop = min(stop, ranges[-1][1])
    if stop <= start:
        return ()

    rows, spans = [], []
    line = 0
    range_index = 0

    def append_span(style, value, handler):
        if not value or not start <= line < stop:
            return
        if spans and spans[-1][0] == style and spans[-1][2] is handler:
            spans[-1][1].append(value)
        else:
            spans.append([style, [value], handler])

    def finish_row():
        nonlocal line, range_index, spans
        while range_index < len(ranges) and line >= ranges[range_index][1]:
            range_index += 1
        if start <= line < stop:
            message = ranges[range_index][2] if range_index < len(ranges) else None
            rows.append(
                (
                    tuple(
                        (style, "".join(parts), handler) if handler else (style, "".join(parts))
                        for style, parts, handler in spans
                    ),
                    message,
                )
            )
        line += 1
        spans = []

    for fragment in ansi:
        style, text = fragment[:2]
        handler = fragment[2] if len(fragment) > 2 else None
        offset = 0
        while offset < len(text):
            newline = text.find("\n", offset)
            if newline < 0:
                append_span(style, text[offset:], handler)
                break
            append_span(style, text[offset:newline], handler)
            finish_row()
            offset = newline + 1

    if line < stop:
        finish_row()
    return tuple(rows)


class MessageRenderer:
    def __init__(self):
        self._lock = RLock()
        self._clear_requested = False
        self.theme = MARKDOWN_THEME
        self.tokens = {}
        self.cache: OrderedDict[tuple[str, str, int, bool], str] = OrderedDict()
        self.cache_limit = 1024
        self.cache_byte_limit = 1_000_000
        self.cache_bytes = 0

    def configure(self, tokens):
        styles = dict(DEFAULT_MARKDOWN_STYLE)
        if tokens.get("foreground"):
            for name in (
                "markdown.text",
                "markdown.paragraph",
                "markdown.list",
                "markdown.item.number",
                "markdown.item.bullet",
            ):
                styles[name] = tokens["foreground"]
        styles.update(tokens.get("markdown", {}))
        self.theme = Theme(styles)
        self.tokens = dict(tokens)
        self.clear_cache()

    def clear_cache(self, *, wait=True):
        """Release all formatted message strings held by this renderer."""
        self._clear_requested = True
        if self._lock.acquire(blocking=wait):
            try:
                self.cache.clear()
                self.cache_bytes = 0
                self._clear_requested = False
            finally:
                self._lock.release()

    @staticmethod
    def _cache_entry_bytes(key, value):
        # Four bytes per code point is a safe UTF-8 upper bound and avoids
        # allocating duplicate byte strings just to account for cache memory.
        return 128 + 4 * (len(key[1]) + len(value))

    def large_code_block(self, text, width, page_rows):
        if len(text) <= MAX_CODE_HIGHLIGHT_CHARS:
            return None
        bounds = _large_fenced_code_range(text)
        return LargeCodeBlock(text, bounds, width, page_rows) if bounds is not None else None

    def code_prefix_rows(self, role, width):
        if role != "reasoning":
            return 0
        label = Text(self.tokens.get("reasoning_label", "Düşünme özeti"))
        console = Console(width=width, force_terminal=False)
        return len(label.wrap(console, max(1, width), overflow="fold"))

    def render_large_code_window(self, role, block, start, stop, *, include_label=False):
        """Format only a bounded visible window from a large plain code block."""
        buffer = StringIO()
        width = block.outer_width
        console = Console(
            file=buffer,
            force_terminal=True,
            color_system="truecolor",
            no_color=False,
            width=width,
            soft_wrap=False,
            theme=self.theme,
            legacy_windows=False,
        )
        background = self.tokens.get(
            role + "_background", "#1c2732" if role == "reasoning" else "#181818"
        )
        if include_label and role == "reasoning":
            console.print(
                Text(
                    self.tokens.get("reasoning_label", "Düşünme özeti"),
                    style=f"{self.tokens.get('reasoning_marker', '#75b5ef')} on {background}",
                )
            )
        rows = block.rows(start, stop)
        if rows:
            content = Text("\n", style="markdown.code").join(rows)
            console.print(Padding(content, (0, 1), style=f"#dedede on {background}", expand=True))
        return buffer.getvalue()

    def transcript(self, messages, width, show_reasoning=True, cancelled=None, active_index=None):
        """Prepare immutable transcript text and line ranges off the UI thread."""
        pieces, ranges = [], []
        line = 0
        ordered = message_order(message.get("role") for message in messages)
        for index in ordered:
            message = messages[index]
            if cancelled and cancelled():
                raise InterruptedError("Sohbet yüklemesi daha yeni bir seçimle değiştirildi")
            if message["role"] == "reasoning" and not show_reasoning:
                continue
            value = self.render(
                message["role"],
                render_chatgpt_annotations(message["text"]),
                width,
                index > 0,
                index == active_index,
            )
            count = value.count("\n")
            ranges.append((line, line + count, index))
            line += count
            pieces.append(value)
        return "".join(pieces), ranges

    def render(
        self,
        role: str,
        text: str,
        width: int,
        separated: bool,
        active: bool,
    ) -> str:
        with self._lock:
            try:
                return self._render_cached(role, text, width, separated, active)
            finally:
                if self._clear_requested:
                    self.clear_cache()

    def _render_cached(self, role, text, width, separated, active):
        key = (role, text, width, separated)
        if not active and key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        buffer = StringIO()
        console = Console(
            file=buffer,
            force_terminal=True,
            color_system="truecolor",
            no_color=False,
            width=width,
            soft_wrap=False,
            theme=self.theme,
            # Preserve OSC-8 targets in the ANSI transcript on Windows too;
            # legacy console detection otherwise strips clickable link data.
            legacy_windows=False,
        )
        defaults = {"user": "#303030", "reasoning": "#1c2732"}
        background = self.tokens.get(role + "_background", defaults.get(role, "#181818"))
        if role == "reasoning":
            console.print(
                Text(
                    self.tokens.get("reasoning_label", "Düşünme özeti"),
                    style=f"{self.tokens.get('reasoning_marker', '#75b5ef')} on {background}",
                )
            )
        if text:
            code = _single_large_fenced_code(text) if len(text) > MAX_CODE_HIGHLIGHT_CHARS else None
            content = (
                Text(code, style="markdown.code", no_wrap=False, overflow="fold")
                if code is not None
                else ChatMarkdown(text, code_theme="monokai")
            )
            console.print(
                Padding(
                    content,
                    (0, 1),
                    style=f"#dedede on {background}",
                    expand=True,
                )
            )
        elif active:
            console.print(
                Padding(
                    Text(
                        self.tokens.get("waiting_label", "Yanıt hazırlanıyor…"), style="dim italic"
                    ),
                    (0, 1),
                    style=f"on {background}",
                )
            )
        console.print()
        value = buffer.getvalue()
        if not active:
            previous = self.cache.pop(key, None)
            if previous is not None:
                self.cache_bytes -= self._cache_entry_bytes(key, previous)
            self.cache[key] = value
            self.cache_bytes += self._cache_entry_bytes(key, value)
            while len(self.cache) > self.cache_limit or self.cache_bytes > self.cache_byte_limit:
                removed_key, removed_value = self.cache.popitem(last=False)
                self.cache_bytes -= self._cache_entry_bytes(removed_key, removed_value)
        return value

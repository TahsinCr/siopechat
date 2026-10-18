"""Rich Markdown rendering and provider annotation presentation."""

from __future__ import annotations

import re
from collections import OrderedDict
from io import StringIO
from typing import Any

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
        lexer = self.lexer_name
        if lexer == "text":
            lexer = detect_code_language(code)
        yield Syntax(code, lexer, theme=self.theme, word_wrap=True, padding=(0, 1))


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
        for key in ("id", "local_pending")
        if key in message and message[key] is not None
    }


class MessageRenderer:
    def __init__(self):
        self.theme = MARKDOWN_THEME
        self.tokens = {}
        self.cache: OrderedDict[tuple[str, str, int, bool], str] = OrderedDict()
        self.cache_limit = 4096
        self.cache_character_limit = 4_000_000
        self.cache_characters = 0

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
        self.cache.clear()
        self.cache_characters = 0

    def render(
        self,
        role: str,
        text: str,
        width: int,
        separated: bool,
        active: bool,
    ) -> str:
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
        )
        defaults = {"user": "#303030", "reasoning": "#1c2732"}
        background = self.tokens.get(role + "_background", defaults.get(role, "#181818"))
        if role == "reasoning":
            console.print(
                Text(
                    "Düşünme özeti",
                    style=f"{self.tokens.get('reasoning_marker', '#75b5ef')} on {background}",
                )
            )
        if text:
            console.print(
                Padding(
                    ChatMarkdown(text, code_theme="monokai"),
                    (0, 1),
                    style=f"#dedede on {background}",
                    expand=True,
                )
            )
        elif active:
            console.print(
                Padding(
                    Text("Yanıt hazırlanıyor…", style="dim italic"),
                    (0, 1),
                    style=f"on {background}",
                )
            )
        console.print()
        value = buffer.getvalue()
        if not active:
            previous = self.cache.pop(key, None)
            if previous is not None:
                self.cache_characters -= len(key[1]) + len(previous)
            self.cache[key] = value
            self.cache_characters += len(key[1]) + len(value)
            while (
                len(self.cache) > self.cache_limit
                or self.cache_characters > self.cache_character_limit
            ):
                removed_key, removed_value = self.cache.popitem(last=False)
                self.cache_characters -= len(removed_key[1]) + len(removed_value)
        return value

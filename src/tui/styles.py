"""Typed terminal and transcript styles shared across the TUI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True, slots=True, kw_only=True)
class StyleItem:
    """A named style with color properties kept separate from text attributes."""

    name: str
    color: str | None = None
    bgcolor: str | None = None
    style: str | None = None

    def to_prompt_toolkit(self) -> str:
        """Serialize this style for prompt_toolkit's Style.from_dict."""
        parts = []
        if self.bgcolor:
            parts.append(f"bg:{self.bgcolor}")
        if self.color:
            parts.append(self.color)
        if self.style:
            parts.extend(self.style.split())
        return " ".join(parts)

    def to_rich(self) -> str:
        """Serialize this style for Rich, keeping foreground and background explicit."""
        parts = []
        if self.style:
            parts.extend(self.style.split())
        if self.color:
            parts.append(self.color)
        if self.bgcolor:
            parts.extend(("on", self.bgcolor))
        return " ".join(parts)


@dataclass(frozen=True, slots=True, kw_only=True)
class IconStyleItem(StyleItem):
    """A named terminal icon that carries its glyph and colors together."""

    icon: str


def prompt_toolkit_style_map(*groups: Mapping[str, StyleItem]) -> dict[str, str]:
    """Build the flat style mapping at the prompt_toolkit boundary."""
    return {item.name: item.to_prompt_toolkit() for group in groups for item in group.values()}


def rich_style_map(*groups: Mapping[str, StyleItem]) -> dict[str, str]:
    """Build the flat style mapping at the Rich boundary."""
    return {item.name: item.to_rich() for group in groups for item in group.values()}


DEFAULT_TAB_WIDTH = 26
DEFAULT_MESSAGE_WIDTH = 100
DEFAULT_TRANSCRIPT_GUTTER_WIDTH = 4
DEFAULT_COMPOSER_MAX_LINES = 6

ICONS: dict[str, IconStyleItem] = {
    "role.user": IconStyleItem(
        name="role.user", color="#69aef2", style="bold", icon=">"
    ),
    "role.assistant": IconStyleItem(
        name="role.assistant", color="#78c9a6", style="bold", icon="◆"
    ),
    "role.reasoning": IconStyleItem(
        name="role.reasoning", color="#b6a2e5", style="bold", icon="◇"
    ),
    "provider.chatgpt": IconStyleItem(
        name="provider.chatgpt", color="#ffffff", style="bold", icon="◎"
    ),
    "provider.gemini": IconStyleItem(
        name="provider.gemini", color="#8ecfff", style="bold", icon="✦"
    ),
    "provider.claude": IconStyleItem(
        name="provider.claude", color="#d99977", style="bold", icon="✳"
    ),
    "provider.generic": IconStyleItem(
        name="provider.generic", color="#b7c8db", icon="◈"
    ),
    "copy": IconStyleItem(name="icon.copy", color="#aeb9c4", icon="⧉"),
    "edit": IconStyleItem(name="icon.edit", color="#aeb9c4", icon="✎"),
    "retry": IconStyleItem(name="icon.retry", color="#8ecfff", icon="↻"),
    "branch": IconStyleItem(name="icon.branch", color="#c3a6ff", icon="⎇"),
    "sources": IconStyleItem(name="icon.sources", color="#79c5d0", icon="↗"),
    "delete": IconStyleItem(name="icon.delete", color="#e58f8f", icon="⌫"),
    "attachment": IconStyleItem(name="icon.attachment", color="#c5cbd2", icon="▤"),
    "add": IconStyleItem(name="icon.add", color="#86d49b", icon="+"),
    "minus": IconStyleItem(name="icon.minus", color="#bcbcbc", icon="-"),
    "dropdown": IconStyleItem(name="icon.dropdown", color="#a6a6a6", icon="▾"),
    "collapsed": IconStyleItem(name="icon.collapsed", color="#a6a6a6", icon="▸"),
    "close": IconStyleItem(name="icon.close", color="#a6a6a6", icon="×"),
    "close_all": IconStyleItem(name="icon.close_all", color="#e58f8f", icon="⊗"),
    "search": IconStyleItem(name="icon.search", color="#aeb9c4", icon="⌕"),
    "filter": IconStyleItem(name="icon.filter", color="#aeb9c4", icon="≡"),
    "menu": IconStyleItem(name="icon.menu", color="#c3a6ff", icon="≣"),
    "split": IconStyleItem(name="icon.split", color="#aeb9c4", icon="◫"),
    "settings": IconStyleItem(name="icon.settings", color="#c3a6ff", icon="⚙"),
    "power": IconStyleItem(name="icon.power", color="#e58f8f", icon="⏻"),
    "stop": IconStyleItem(name="icon.stop", color="#e58f8f", icon="■"),
    "up": IconStyleItem(name="icon.up", color="#aeb9c4", icon="↑"),
    "down": IconStyleItem(name="icon.down", color="#aeb9c4", icon="↓"),
    "left": IconStyleItem(name="icon.left", color="#aeb9c4", icon="←"),
    "right": IconStyleItem(name="icon.right", color="#aeb9c4", icon="→"),
    "back": IconStyleItem(name="icon.back", color="#aeb9c4", icon="‹"),
    "forward": IconStyleItem(name="icon.forward", color="#aeb9c4", icon="›"),
    "next": IconStyleItem(name="icon.next", color="#aeb9c4", icon="↳"),
    "info": IconStyleItem(name="icon.info", color="#8ecfff", icon="ⓘ"),
    "running": IconStyleItem(name="icon.running", color="#f0ad66", icon="●"),
    "complete": IconStyleItem(name="icon.complete", color="#86d49b", icon="✓"),
    "idle": IconStyleItem(name="icon.idle", color="#9ca3af", icon="◌"),
    "disabled": IconStyleItem(name="icon.disabled", color="#777777", icon="○"),
    "spinner": IconStyleItem(name="icon.spinner", color="#8ecfff", icon="|/-\\"),
}

DEFAULT_MARKDOWN_STYLES: dict[str, StyleItem] = {
    "markdown.text": StyleItem(name="markdown.text", color="#dce3e8"),
    "markdown.paragraph": StyleItem(name="markdown.paragraph", color="#dce3e8"),
    "markdown.h1": StyleItem(name="markdown.h1", color="#f0f4f7", style="bold"),
    "markdown.h2": StyleItem(name="markdown.h2", color="#c9dce8", style="bold"),
    "markdown.h3": StyleItem(name="markdown.h3", color="#b8cedd", style="bold"),
    "markdown.h4": StyleItem(name="markdown.h4", color="#d2dce3", style="bold"),
    "markdown.h5": StyleItem(name="markdown.h5", color="#c4d0d8", style="bold"),
    "markdown.h6": StyleItem(name="markdown.h6", color="#b7c3cc", style="bold"),
    "markdown.link": StyleItem(name="markdown.link", color="#79c5d0", style="underline"),
    "markdown.link_url": StyleItem(name="markdown.link_url", color="#79c5d0", style="underline"),
    "markdown.code": StyleItem(name="markdown.code", color="#d7c2e9"),
    "markdown.block_quote": StyleItem(name="markdown.block_quote", color="#b0bdc7"),
    "markdown.hr": StyleItem(name="markdown.hr", color="#52616d"),
    "markdown.list": StyleItem(name="markdown.list", color="#dce3e8"),
    "markdown.item.bullet": StyleItem(name="markdown.item.bullet", color="#a9c6d6"),
    "markdown.item.number": StyleItem(name="markdown.item.number", color="#a9c6d6"),
    "markdown.table.border": StyleItem(name="markdown.table.border", color="#52616d"),
    "markdown.table.header": StyleItem(name="markdown.table.header", color="#dce8ef", style="bold"),
}

DEFAULT_ROLE_STYLES: dict[str, StyleItem] = {
    "user": StyleItem(name="message.user", color="#dce6f2", bgcolor="#2d343c"),
    "assistant": StyleItem(name="message.assistant", color="#e1e8e3", bgcolor="#2d3330"),
    "reasoning": StyleItem(name="message.reasoning", color="#d2d8e4", bgcolor="#292e37"),
}

DEFAULT_MESSAGE_STYLE = StyleItem(name="message.default", color="#dedede", bgcolor="#181818")
DEFAULT_REASONING_LABEL_STYLE = StyleItem(
    name="reasoning.label",
    color=DEFAULT_ROLE_STYLES["reasoning"].color,
    bgcolor=DEFAULT_ROLE_STYLES["reasoning"].bgcolor,
    style="bold",
)

DEFAULT_TERMINAL_STYLES: dict[str, StyleItem] = {
    "sidebar.open": StyleItem(name="sidebar.open", color="#ffffff", style="bold"),
    "sidebar.running": StyleItem(name="sidebar.running", color="#86d49b", style="bold"),
    "tab.open": StyleItem(name="tab.open", color="#ffffff", style="bold"),
    "sync": StyleItem(name="sync", color="#8ecfff"),
    "sync.error": StyleItem(name="sync.error", color="#f0ad66"),
    "connection.connected": StyleItem(name="connection.connected", color="#86d49b", style="bold"),
    "connection.disconnected": StyleItem(name="connection.disconnected", color="#939393"),
    "settings.enabled": StyleItem(name="settings.enabled", color="#86d49b", style="bold"),
    "tabs.border": StyleItem(name="tabs.border", color="#42576a", bgcolor="#202020"),
    "tab.running": StyleItem(name="tab.running", color="#86d49b", bgcolor="#202020"),
    "pane.header": StyleItem(name="pane.header", color="#b7c8db", bgcolor="#272c31"),
    "settings.search": StyleItem(name="settings.search", color="#d8e3ee", bgcolor="#292d32"),
    "settings.editor": StyleItem(name="settings.editor", color="#ffffff", bgcolor="#30363d"),
    "settings.value": StyleItem(name="settings.value", color="#90b6d4"),
    "settings.conflict": StyleItem(name="settings.conflict", color="#ff6868", style="bold"),
    "settings": StyleItem(name="settings", color="#dedede", bgcolor="#202020"),
    "settings.title": StyleItem(name="settings.title", color="#c3a6ff", style="bold"),
    "downloads.header": StyleItem(name="downloads.header", bgcolor="#292929"),
    "downloads.title": StyleItem(
        name="downloads.title",
        color="#e5e5e5",
        bgcolor="#292929",
        style="bold",
    ),
    "downloads.summary": StyleItem(name="downloads.summary", color="#9ca3af", bgcolor="#292929"),
    "downloads.toolbar": StyleItem(name="downloads.toolbar", bgcolor="#262626"),
    "downloads.actions": StyleItem(name="downloads.actions", bgcolor="#262626"),
    "downloads.list": StyleItem(name="downloads.list", bgcolor="#202020"),
    "downloads.group": StyleItem(
        name="downloads.group",
        color="#d4d4d4",
        bgcolor="#292929",
        style="bold",
    ),
    "downloads.file": StyleItem(name="downloads.file", color="#dedede", bgcolor="#2c2c2c"),
    "downloads.filename": StyleItem(name="downloads.filename", color="#eeeeee", style="bold"),
    "downloads.source": StyleItem(name="downloads.source", color="#a6a6a6", bgcolor="#242424"),
    "downloads.selected": StyleItem(name="downloads.selected", color="#ffffff", bgcolor="#414141"),
    "downloads.empty": StyleItem(name="downloads.empty", bgcolor="#202020"),
    "downloads.empty.title": StyleItem(name="downloads.empty.title", color="#d4d4d4", style="bold"),
    "downloads.empty.detail": StyleItem(name="downloads.empty.detail", color="#929292"),
    "downloads.status": StyleItem(name="downloads.status", color="#9ca3af", bgcolor="#242424"),
    "settings.nav": StyleItem(name="settings.nav", color="#b7b7b7", bgcolor="#252525"),
    "settings.category": StyleItem(name="settings.category", color="#b7b7b7", bgcolor="#252525"),
    "settings.category.active": StyleItem(
        name="settings.category.active",
        color="#e0f3ff",
        bgcolor="#294858",
        style="bold",
    ),
    "message.selected": StyleItem(name="message.selected", color="#ffffff", bgcolor="#414141"),
    "message.toolbar.user": StyleItem(
        name="message.toolbar.user",
        color="#c4c4c4",
        bgcolor="#2d343c",
    ),
    "message.toolbar.assistant": StyleItem(
        name="message.toolbar.assistant",
        color="#bcbcbc",
        bgcolor="#2d3330",
    ),
    "message.toolbar.action": StyleItem(
        name="message.toolbar.action",
        color="#ededed",
        bgcolor="#454545",
    ),
    "source.list.url": StyleItem(name="source.list.url", color="#79c5d0", style="underline"),
    "source.list.title": StyleItem(name="source.list.title", color="#dce3e8", style="bold"),
    "source.list.description": StyleItem(name="source.list.description", color="#a6a6a6"),
    "message.toolbar.version": StyleItem(
        name="message.toolbar.version",
        color="#d3d3d3",
        bgcolor="#383838",
    ),
    "message.toolbar.version.disabled": StyleItem(
        name="message.toolbar.version.disabled",
        color="#777777",
        bgcolor="#383838",
    ),
    "message.toolbar.metadata": StyleItem(
        name="message.toolbar.metadata",
        color="#a6a6a6",
        bgcolor="#303030",
    ),
    "message.file": StyleItem(
        name="message.file",
        color="#679bd4",
        bgcolor="#2d3330",
        style="underline",
    ),
    "chat.context": StyleItem(name="chat.context", color="#a4b5c7"),
    "brand": StyleItem(name="brand", color="#c3a6ff", bgcolor="#252525", style="bold"),
    "transcript": StyleItem(name="transcript", color="#dedede", bgcolor="#202020"),
    "metadata.project": StyleItem(
        name="metadata.project",
        color="#679bd4",
        bgcolor="#242424",
        style="bold",
    ),
    "metadata.temporary": StyleItem(
        name="metadata.temporary",
        color="#c3a6ff",
        bgcolor="#242424",
        style="bold",
    ),
    "tab.divider": StyleItem(name="tab.divider", color="#454545", bgcolor="#202020"),
    "metadata.effort": StyleItem(
        name="metadata.effort",
        color="#86d49b",
        bgcolor="#242424",
        style="bold",
    ),
    "tab.close": StyleItem(name="tab.close", color="#929292"),
    "selection": StyleItem(name="selection", color="#e1e1e1", style="bold"),
    "sidebar": StyleItem(name="sidebar", color="#c7c7c7", bgcolor="#252525"),
    "": StyleItem(name="", color="#dedede", bgcolor="#202020"),
    "toolbar": StyleItem(name="toolbar", color="#dedede", bgcolor="#262626"),
    "button": StyleItem(name="button", color="#bcbcbc"),
    "button.focused": StyleItem(name="button.focused", color="#ffffff", bgcolor="#414141"),
    "title": StyleItem(name="title", color="#eeeeee", style="bold"),
    "chat.header": StyleItem(name="chat.header", color="#c7c7c7", bgcolor="#242424"),
    "scrollbar.background": StyleItem(name="scrollbar.background", bgcolor="#242424"),
    "scrollbar.button": StyleItem(name="scrollbar.button", bgcolor="#505050"),
    "sidebar.header": StyleItem(
        name="sidebar.header",
        color="#d4d4d4",
        bgcolor="#252525",
        style="bold",
    ),
    "sidebar.search": StyleItem(name="sidebar.search", color="#dedede", bgcolor="#202020"),
    "sidebar.selected": StyleItem(
        name="sidebar.selected",
        color="#ffffff",
        bgcolor="#393939",
        style="bold",
    ),
    "sidebar.item": StyleItem(name="sidebar.item", color="#ffffff"),
    "sidebar.muted": StyleItem(name="sidebar.muted", color="#929292"),
    "tabs": StyleItem(name="tabs", color="#999999", bgcolor="#202020"),
    "tab.active": StyleItem(name="tab.active", color="#a9cfff", bgcolor="#202020", style="bold"),
    "tab": StyleItem(name="tab", color="#929292", bgcolor="#202020"),
    "tabs.help": StyleItem(name="tabs.help", color="#929292", bgcolor="#202020"),
    "palette.search": StyleItem(name="palette.search", color="#eeeeee", bgcolor="#202020"),
    "palette.selected": StyleItem(
        name="palette.selected",
        color="#ffffff",
        bgcolor="#393939",
        style="bold",
    ),
    "palette.item": StyleItem(name="palette.item", color="#c7c7c7"),
    "separator": StyleItem(name="separator", color="#6b7280"),
    "activity": StyleItem(name="activity", color="#9ca3af"),
    "composer": StyleItem(name="composer", color="#f9fafb", bgcolor="#292929"),
    "prompt": StyleItem(name="prompt", color="#f9fafb", bgcolor="#292929", style="bold"),
    "placeholder": StyleItem(name="placeholder", color="#9ca3af", bgcolor="#292929"),
    "metadata": StyleItem(name="metadata", color="#9ca3af", bgcolor="#272727"),
    "metadata.model": StyleItem(
        name="metadata.model",
        color="#f0ad66",
        bgcolor="#242424",
        style="bold",
    ),
    "metadata.ready": StyleItem(name="metadata.ready", color="#86efac", bgcolor="#272727"),
    "help": StyleItem(name="help", color="#9ca3af", bgcolor="#242424"),
    "dialog.body": StyleItem(name="dialog.body", color="#dedede", bgcolor="#252525"),
    "dialog.body label": StyleItem(name="dialog.body label", color="#dedede"),
    "dialog.body button": StyleItem(name="dialog.body button", color="#dedede", bgcolor="#303030"),
    "dialog.body button.focused": StyleItem(
        name="dialog.body button.focused",
        color="#ffffff",
        bgcolor="#414141",
        style="bold",
    ),
    "dialog.body frame.label": StyleItem(
        name="dialog.body frame.label",
        color="#c3a6ff",
        style="bold",
    ),
    "checkbox": StyleItem(name="checkbox", color="#bcbcbc"),
    "checkbox-selected": StyleItem(name="checkbox-selected", color="#ffffff", bgcolor="#393939"),
    "checkbox-checked": StyleItem(name="checkbox-checked", color="#86d49b"),
    "radio": StyleItem(name="radio", color="#bcbcbc"),
    "radio-selected": StyleItem(name="radio-selected", color="#ffffff", bgcolor="#393939"),
    "radio-checked": StyleItem(name="radio-checked", color="#86d49b"),
    "frame.label": StyleItem(name="frame.label", color="#d4d4d4"),
    "frame.border": StyleItem(name="frame.border", color="#505050"),
    "resume.selected": StyleItem(
        name="resume.selected",
        color="#ffffff",
        bgcolor="#414141",
        style="bold",
    ),
    "resume.item": StyleItem(name="resume.item", color="#d1d5db"),
    "resume.time": StyleItem(name="resume.time", color="#9ca3af"),
    "completion-menu": StyleItem(name="completion-menu", color="#e5e7eb", bgcolor="#303030"),
    "completion-menu.completion": StyleItem(
        name="completion-menu.completion",
        color="#e5e7eb",
        bgcolor="#303030",
    ),
    "completion-menu.completion.current": StyleItem(
        name="completion-menu.completion.current",
        color="#ffffff",
        bgcolor="#414141",
        style="bold",
    ),
    "completion-menu.meta.completion": StyleItem(
        name="completion-menu.meta.completion",
        color="#9ca3af",
        bgcolor="#303030",
    ),
    "completion-menu.meta.completion.current": StyleItem(
        name="completion-menu.meta.completion.current",
        color="#eeeeee",
        bgcolor="#414141",
    ),
}

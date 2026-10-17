"""Declarative semantic tokens; themes contain no executable code."""

import json
import re
from pathlib import Path


class ThemeService:
    DEFAULT = {
        "background": "#202020",
        "foreground": "#dedede",
        "accent": "#c5c5c5",
        "error": "#ff6666",
    }

    def __init__(self):
        self._themes: dict[str, tuple[str, dict[str, str]]] = {}
        self.failures: dict[str, str] = {}

    def register(self, owner: str, theme_id: str, tokens: dict[str, str]) -> None:
        if not theme_id or theme_id == "default" or theme_id in self._themes:
            raise ValueError("invalid or duplicate theme ID")
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in tokens.items()):
            raise ValueError("theme tokens must be strings")
        if any(not re.fullmatch(r"#[0-9a-fA-F]{6}", value) for value in tokens.values()):
            raise ValueError("theme colors must be six-digit hexadecimal colors")
        self._themes[theme_id] = (owner, dict(tokens))

    def unregister(self, owner: str) -> None:
        self._themes = {key: value for key, value in self._themes.items() if value[0] != owner}

    def resolve(self, theme_id: str) -> dict[str, str]:
        return self.DEFAULT | self._themes.get(theme_id, ("", {}))[1]

    def available(self) -> tuple[str, ...]:
        return ("default", *self._themes)

    def load_directory(self, directory: Path) -> None:
        for path in sorted(directory.glob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(value, dict) or not isinstance(value.get("id"), str):
                    raise ValueError("invalid theme")
                self.register(f"theme:{path.stem}", value["id"], value["tokens"])
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                self.failures[path.name] = "invalid_theme"


DEFAULT_MARKDOWN_STYLE = {
    "markdown.text": "#e5e7eb",
    "markdown.paragraph": "#e5e7eb",
    "markdown.h1": "bold #f3f4f6",
    "markdown.h2": "bold #f3f4f6",
    "markdown.h3": "bold #f3f4f6",
    "markdown.h4": "bold #e5e7eb",
    "markdown.h5": "bold #d1d5db",
    "markdown.h6": "bold #d1d5db",
    "markdown.link": "underline #22d3ee",
    "markdown.link_url": "underline #22d3ee",
    "markdown.code": "#22d3ee",
    "markdown.block_quote": "#9ca3af",
    "markdown.hr": "#606060",
    "markdown.list": "#e5e7eb",
    "markdown.item.bullet": "#e5e7eb",
    "markdown.item.number": "#e5e7eb",
}

DEFAULT_TERMINAL_STYLE = {
    "connection.connected": "bold #86d49b",
    "connection.disconnected": "#939393",
    "settings.enabled": "bold #86d49b",
    "tabs.border": "bg:#202020 #42576a",
    "tab.running": "bg:#202020 #86d49b",
    "settings.search": "bg:#292d32 #d8e3ee",
    "settings.editor": "bg:#30363d #ffffff",
    "settings.value": "#90b6d4",
    "settings": "bg:#202020 #dedede",
    "settings.title": "bold #c3a6ff",
    "settings.nav": "bg:#252525 #b7b7b7",
    "settings.category": "bg:#252525 #b7b7b7",
    "settings.category.active": "bg:#294858 bold #e0f3ff",
    "message.selected": "bg:#34516b #ffffff",
    "chat.context": "#a4b5c7",
    "brand": "bg:#252525 bold #c3a6ff",
    "transcript": "bg:#202020 #dedede",
    "metadata.project": "bg:#242424 bold #679bd4",
    "tab.divider": "bg:#202020 #454545",
    "role.user": "bg:#202020 bold #86d49b",
    "role.reasoning": "bg:#202020 #75b5ef",
    "role.assistant": "bg:#202020 #f0ad66",
    "metadata.effort": "bg:#242424 bold #86d49b",
    "tab.close": "#929292",
    "selection": "bold #e1e1e1",
    "sidebar": "bg:#252525 #c7c7c7",
    "": "bg:#202020 #dedede",
    "toolbar": "bg:#262626 #dedede",
    "button": "#bcbcbc",
    "button.focused": "bg:#414141 #ffffff",
    "title": "bold #eeeeee",
    "chat.header": "bg:#242424 #c7c7c7",
    "scrollbar.background": "bg:#242424",
    "scrollbar.button": "bg:#505050",
    "sidebar.header": "bg:#252525 bold #d4d4d4",
    "sidebar.search": "bg:#202020 #dedede",
    "sidebar.selected": "bg:#393939 #ffffff bold",
    "sidebar.item": "#c7c7c7",
    "sidebar.muted": "#929292",
    "tabs": "bg:#202020 #999999",
    "tab.active": "bg:#202020 bold #a9cfff",
    "tab": "bg:#202020 #929292",
    "tabs.help": "bg:#202020 #929292",
    "palette.search": "bg:#202020 #eeeeee",
    "palette.selected": "bg:#393939 #ffffff bold",
    "palette.item": "#c7c7c7",
    "separator": "#6b7280",
    "activity": "#9ca3af",
    "composer": "bg:#292929 #f9fafb",
    "prompt": "bg:#292929 bold #f9fafb",
    "placeholder": "bg:#292929 #9ca3af",
    "metadata": "bg:#272727 #9ca3af",
    "metadata.model": "bg:#242424 bold #f0ad66",
    "metadata.ready": "bg:#272727 #86efac",
    "help": "bg:#242424 #9ca3af",
    "frame.label": "#d4d4d4",
    "frame.border": "#505050",
    "resume.selected": "bg:#414141 #ffffff bold",
    "resume.item": "#d1d5db",
    "resume.time": "#9ca3af",
    "completion-menu": "bg:#303030 #e5e7eb",
    "completion-menu.completion": "bg:#303030 #e5e7eb",
    "completion-menu.completion.current": "bg:#414141 #ffffff bold",
    "completion-menu.meta.completion": "bg:#303030 #9ca3af",
    "completion-menu.meta.completion.current": "bg:#414141 #eeeeee",
}

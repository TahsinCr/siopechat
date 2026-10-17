"""Validated shared settings with atomic JSON persistence."""

import copy
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from utils.events import EventBus

DEFAULT_KEYBINDINGS = {
    "edit.select_all": "c-a",
    "edit.cut": "c-x",
    "chat.temporary": "escape t",
    "ui.command_palette": "c-p",
    "ui.search": "c-f",
    "navigation.focus_sidebar": "c-g",
    "ui.toggle_sidebar": "f7",
    "layout.split": "escape v",
    "layout.split_down": "escape h",
    "layout.unsplit": "escape u",
    "chat.attach": "escape a",
    "ui.filter": "escape f",
    "accounts.add": "escape n",
    "chat.new": "c-t",
    "chat.close_tab": "c-w",
    "chat.next_tab": "escape right",
    "chat.previous_tab": "escape left",
    "chat.resume": "c-r",
    "chat.send": "enter",
    "chat.newline": "escape enter",
    "chat.newline_shift": "escape c-j",
    "chat.newline_lf": "c-j",
    "chat.paste_image": "c-v",
    "chat.interrupt": "c-c",
    "chat.clear_transcript": "c-l",
    "chat.reload": "f8",
    "chat.copy_response": "f2",
    "chat.remove_attachment": "f3",
    "command.help": "f1",
    "command.rename": "f4",
    "command.projects": "f6",
    "projects.create": "escape p",
    "command.model": "escape m",
    "command.effort": "escape e",
    "command.project": "f9",
    "command.refresh": "f10",
    "command.settings": "f5",
    "command.session": "f12",
    "chat.scroll_top": "c-home",
    "chat.scroll_bottom": "c-end",
    "app.quit": "c-q",
    "app.quit_eof": "c-d",
    "navigation.sidebar_up": "up",
    "navigation.sidebar_down": "down",
    "navigation.sidebar_toggle_group": "space",
    "navigation.close_overlay": "escape",
    "navigation.composer_next": "tab",
    "navigation.focus_composer": "s-tab",
    "navigation.transcript_up": "pageup",
    "navigation.transcript_down": "pagedown",
    "navigation.transcript_start": "home",
    "navigation.transcript_end": "end",
}

DEFAULTS = {
    "version": 1,
    "application": {
        "language": "en",
        "theme": "default",
        "model": "",
        "effort": "medium",
        "mouse": True,
        "show_reasoning": True,
    },
    "keybindings": DEFAULT_KEYBINDINGS,
    "providers": {"default": "chatgpt"},
    "extensions": {"layout": {"enabled": True}},
    "accounts": {
        "active": "chatgpt/default",
        "items": [{"provider": "chatgpt", "id": "default", "label": "ChatGPT", "port": 9222}],
    },
}


@dataclass(frozen=True)
class SettingField:
    key: str
    value_type: type
    default: Any
    title_key: str
    choices: tuple[Any, ...] = ()

    def validate(self, value: Any) -> None:
        if type(value) is not self.value_type or (self.choices and value not in self.choices):
            raise ValueError(f"invalid value for setting {self.key}")
        json.dumps(value, allow_nan=False)


def _merge(defaults: dict, current: dict) -> dict:
    result = copy.deepcopy(defaults)
    for key, value in current.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


@dataclass(frozen=True)
class SettingsChanged:
    section: str
    keys: tuple[str, ...]


class Settings:
    def __init__(self, path: Path, events: EventBus | None = None):
        self.events = events
        self.path = Path(path)
        self._fields: dict[str, tuple[SettingField, ...]] = {}
        self._lock = threading.RLock()
        self._data = copy.deepcopy(DEFAULTS)
        if self.path.exists():
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("config.json must contain an object")
            self._data = _merge(DEFAULTS, value)
        self._validate_document(self._data)

    @staticmethod
    def _validate_document(data: dict) -> None:
        if type(data.get("version")) is not int or data["version"] != 1:
            raise ValueError("unsupported settings version")
        for section in ("application", "keybindings", "providers", "extensions", "accounts"):
            if not isinstance(data.get(section), dict):
                raise ValueError(f"settings section {section} must be an object")
        accounts = data["accounts"]
        items = accounts.get("items")
        if not isinstance(items, list) or not items:
            raise ValueError("accounts must contain at least one account")
        seen = set()
        for item in items:
            if (
                not isinstance(item, dict)
                or item.get("provider") not in ("chatgpt", "gemini")
                or not isinstance(item.get("id"), str)
                or not re.fullmatch("[A-Za-z0-9_-]+", item["id"])
                or not isinstance(item.get("label"), str)
                or not item["label"].strip()
                or type(item.get("port")) is not int
                or not 1024 <= item["port"] <= 65535
            ):
                raise ValueError("invalid account metadata")
            key = item["provider"] + "/" + item["id"]
            if key in seen:
                raise ValueError("duplicate account")
            seen.add(key)
        if accounts.get("active") not in seen:
            raise ValueError("active account does not exist")
        for key in ("language", "theme", "effort"):
            if not isinstance(data["application"].get(key), str):
                raise ValueError(f"invalid application setting {key}")
        if (
            not isinstance(data["application"].get("model", ""), str)
            or type(data["application"].get("mouse", True)) is not bool
            or type(data["application"].get("show_reasoning", True)) is not bool
        ):
            raise ValueError("invalid model/mouse preference")
        for value in data["extensions"].values():
            if not isinstance(value, dict) or type(value.get("enabled", True)) is not bool:
                raise ValueError("invalid extension settings")
        if not all(
            isinstance(k, str) and isinstance(v, str) and v.strip()
            for k, v in data["keybindings"].items()
        ):
            raise ValueError("keybindings must map action IDs to key strings")
        from prompt_toolkit.key_binding import KeyBindings

        sequences = []
        for value in data["keybindings"].values():
            registry = KeyBindings()
            registry.add(*value.lower().split())(lambda _: None)
            sequences.append(registry.bindings[0].keys)
        if len(sequences) != len(set(sequences)):
            raise ValueError("keybinding sequences must be unique")
        json.dumps(data, allow_nan=False)

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def register(self, owner: str, fields: tuple[SettingField, ...]) -> None:
        with self._lock:
            if owner in self._fields or not owner:
                raise ValueError("settings owner already registered or empty")
            if len({field.key for field in fields}) != len(fields):
                raise ValueError("duplicate setting key")
            current = self._data["extensions"].get(owner, {})
            for field in fields:
                field.validate(current.get(field.key, field.default))
            self._fields[owner] = fields

    def unregister(self, owner: str) -> None:
        self._fields.pop(owner, None)  # Preserve stored user values on disable.

    def fields(self) -> dict[str, tuple[SettingField, ...]]:
        return self._fields.copy()

    def extension(self, owner: str) -> dict:
        with self._lock:
            defaults = {f.key: f.default for f in self._fields.get(owner, ())}
            return _merge(defaults, self._data["extensions"].get(owner, {}))

    def update_extension(self, owner: str, values: dict) -> None:
        with self._lock:
            updated = _merge(self.extension(owner), values)
            for field in self._fields.get(owner, ()):
                field.validate(updated.get(field.key, field.default))
            candidate = self.snapshot()
            candidate["extensions"][owner] = updated
            self._commit(candidate)
        self._notify(f"extensions.{owner}", values)

    def update(self, section: str, values: dict) -> None:
        if section not in ("application", "keybindings", "providers", "accounts"):
            raise ValueError("use update_extension for extension settings")
        with self._lock:
            candidate = self.snapshot()
            candidate[section] = _merge(candidate[section], values)
            self._commit(candidate)
        self._notify(section, values)

    def _notify(self, section: str, values: dict) -> None:
        if self.events is not None and not self.events.closed:
            self.events.publish(SettingsChanged(section, tuple(values)))

    def save(self) -> None:
        with self._lock:
            self._commit(self._data)

    def _commit(self, candidate: dict) -> None:
        self._validate_document(candidate)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=".config-",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(candidate, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self._data = copy.deepcopy(candidate)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

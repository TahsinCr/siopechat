"""Validated shared settings with atomic JSON persistence."""

from __future__ import annotations

import copy
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from eventflow import EventBus

from accounts.shared import PROVIDERS
from utils.files import atomic_json_write

_VALID_KEYS = frozenset(
    {
        "c-0",
        "c-1",
        "c-2",
        "c-3",
        "c-4",
        "c-5",
        "c-6",
        "c-7",
        "c-8",
        "c-9",
        "c-@",
        "c-\\",
        "c-]",
        "c-^",
        "c-_",
        "c-a",
        "c-b",
        "c-c",
        "c-d",
        "c-delete",
        "c-down",
        "c-e",
        "c-end",
        "c-f",
        "c-f1",
        "c-f2",
        "c-f3",
        "c-f4",
        "c-f5",
        "c-f6",
        "c-f7",
        "c-f8",
        "c-f9",
        "c-f10",
        "c-f11",
        "c-f12",
        "c-f13",
        "c-f14",
        "c-f15",
        "c-f16",
        "c-f17",
        "c-f18",
        "c-f19",
        "c-f20",
        "c-f21",
        "c-f22",
        "c-f23",
        "c-f24",
        "c-g",
        "c-h",
        "c-home",
        "c-i",
        "c-insert",
        "c-j",
        "c-k",
        "c-l",
        "c-left",
        "c-m",
        "c-n",
        "c-o",
        "c-p",
        "c-pagedown",
        "c-pageup",
        "c-q",
        "c-r",
        "c-right",
        "c-s",
        "c-s-0",
        "c-s-1",
        "c-s-2",
        "c-s-3",
        "c-s-4",
        "c-s-5",
        "c-s-6",
        "c-s-7",
        "c-s-8",
        "c-s-9",
        "c-s-delete",
        "c-s-down",
        "c-s-end",
        "c-s-home",
        "c-s-insert",
        "c-s-left",
        "c-s-pagedown",
        "c-s-pageup",
        "c-s-right",
        "c-s-up",
        "c-t",
        "c-u",
        "c-up",
        "c-v",
        "c-w",
        "c-x",
        "c-y",
        "c-z",
        "delete",
        "down",
        "end",
        "escape",
        "f1",
        "f2",
        "f3",
        "f4",
        "f5",
        "f6",
        "f7",
        "f8",
        "f9",
        "f10",
        "f11",
        "f12",
        "f13",
        "f14",
        "f15",
        "f16",
        "f17",
        "f18",
        "f19",
        "f20",
        "f21",
        "f22",
        "f23",
        "f24",
        "home",
        "insert",
        "left",
        "pagedown",
        "pageup",
        "right",
        "s-delete",
        "s-down",
        "s-end",
        "s-escape",
        "s-home",
        "s-insert",
        "s-left",
        "s-pagedown",
        "s-pageup",
        "s-right",
        "s-tab",
        "s-up",
        "up",
    }
)

_KEY_ALIASES = {
    "backspace": "c-h",
    "c-space": "c-@",
    "enter": "c-m",
    "tab": "c-i",
    "s-c-left": "c-s-left",
    "s-c-right": "c-s-right",
    "s-c-home": "c-s-home",
    "s-c-end": "c-s-end",
}

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
    "layout.next_pane": "escape ]",
    "layout.previous_pane": "escape [",
    "chat.attach": "escape a",
    "ui.filter": "escape f",
    "accounts.add": "escape n",
    "accounts.edit": "escape c",
    "accounts.remove": "escape backspace",
    "chat.new": "c-t",
    "chat.close_tab": "c-w",
    "chat.close_all_tabs": "escape w",
    "chat.next_tab": "escape right",
    "chat.previous_tab": "escape left",
    "chat.resume": "c-r",
    "chat.send": "enter",
    "chat.newline": "escape enter",
    "chat.newline_shift": "escape c-j",
    "chat.newline_lf": "c-j",
    "edit.paste": "c-v",
    "edit.copy": "",
    "chat.interrupt": "c-c",
    "chat.stop": "escape escape",
    "chat.clear_transcript": "c-l",
    "chat.reload": "f8",
    "chat.copy_response": "f2",
    "chat.switch_saved_draft": "escape s",
    "chat.remove_attachment": "f3",
    "command.help": "f1",
    "command.rename": "f4",
    "command.projects": "f6",
    "projects.create": "escape p",
    "projects.edit": "escape r",
    "projects.add_source": "escape i",
    "projects.remove_source": "escape o",
    "chat.delete": "escape d",
    "projects.delete": "escape delete",
    "command.model": "escape m",
    "command.effort": "escape e",
    "command.project": "f9",
    "command.refresh": "f10",
    "command.settings": "f5",
    "command.downloads": "escape j",
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
    "navigation.focus_transcript": "",
    "command.settings.keybindings": "",
    "command.settings.accounts": "",
    "accounts.verify_profile": "",
    "accounts.logout": "",
    "chat.retry": "",
}

IMMUTABLE_KEYBINDINGS = frozenset({"app.quit", "app.quit_eof"})
SUPPORTED_LANGUAGES = ("en", "tr", "ru")


def _parse_key_token(token: str) -> str:
    key = _KEY_ALIASES.get(token, token)
    if key == "space":
        return " "
    if key in _VALID_KEYS or len(key) == 1:
        return key
    raise ValueError(f"Invalid key: {token}")


def keybinding_conflicts(keybindings: dict[str, str]) -> frozenset[str]:
    """Validate key sequences and return every action participating in a collision."""
    by_sequence: dict[tuple[str, ...], list[str]] = {}
    for action_id, value in keybindings.items():
        if not isinstance(action_id, str) or not action_id.strip() or not isinstance(value, str):
            raise ValueError("keybindings must map action IDs to key strings")
        tokens = value.strip().lower().split()
        if not tokens:
            continue
        try:
            sequence = tuple(_parse_key_token(token) for token in tokens)
        except ValueError as error:
            raise ValueError(f"invalid keybinding for {action_id}") from error
        by_sequence.setdefault(sequence, []).append(action_id)
    return frozenset(
        action_id
        for action_ids in by_sequence.values()
        if len(action_ids) > 1
        for action_id in action_ids
    )


DEFAULTS = {
    "version": 1,
    "application": {
        "language": "en",
        "model": "",
        "effort": "medium",
        "mouse": True,
        "show_reasoning": True,
        "download_directory": "downloads",
        "download_max_bytes": 512 * 1024 * 1024,
        "download_allow_private": False,
    },
    "keybindings": DEFAULT_KEYBINDINGS,
    "providers": {key: {"enabled": True} for key in PROVIDERS},
    "accounts": {
        "active": "",
        "selected": {},
        "preferences": {},
        "items": [],
    },
}


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
        self._lock = threading.RLock()
        self._data = copy.deepcopy(DEFAULTS)
        if self.path.exists():
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("config.json must contain an object")
            if isinstance(value.get("application"), dict):
                value["application"] = {
                    key: item
                    for key, item in value["application"].items()
                    if key in DEFAULTS["application"]
                }
            self._data = _merge(DEFAULTS, value)
        items = self._data["accounts"].get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    item.setdefault("enabled", True)
        self._validate_document(self._data)

    @staticmethod
    def _validate_document(data: dict) -> None:
        if type(data.get("version")) is not int or data["version"] != 1:
            raise ValueError("unsupported settings version")
        for section in ("application", "keybindings", "providers", "accounts"):
            if not isinstance(data.get(section), dict):
                raise ValueError(f"settings section {section} must be an object")
        if set(data["application"]) - DEFAULTS["application"].keys():
            raise ValueError("unknown application settings")
        providers = data["providers"]
        if not providers or not all(
            isinstance(v, dict) and type(v.get("enabled")) is bool for v in providers.values()
        ):
            raise ValueError("providers must define enabled flags")
        if not any(v["enabled"] for v in providers.values()):
            raise ValueError("at least one provider must remain enabled")
        preferences = data["accounts"].get("preferences", {})
        if not isinstance(preferences, dict) or not all(
            isinstance(v, dict)
            and isinstance(v.get("model", ""), str)
            and (v.get("effort") is None or isinstance(v.get("effort"), str))
            for v in preferences.values()
        ):
            raise ValueError("invalid account model preferences")
        accounts = data["accounts"]
        items = accounts.get("items")
        if not isinstance(items, list):
            raise ValueError("accounts must be a list")
        seen = set()
        for item in items:
            if (
                not isinstance(item, dict)
                or item.get("provider") not in PROVIDERS
                or not isinstance(item.get("id"), str)
                or not re.fullmatch("[A-Za-z0-9_-]+", item["id"])
                or not isinstance(item.get("label"), str)
                or not item["label"].strip()
                or type(item.get("enabled", True)) is not bool
                or type(item.get("port")) is not int
                or not 1024 <= item["port"] <= 65535
            ):
                raise ValueError("invalid account metadata")
            key = item["provider"] + "/" + item["id"]
            if key in seen:
                raise ValueError("duplicate account")
            seen.add(key)
        by_key = {x["provider"] + "/" + x["id"]: x for x in items}
        for item in items:
            target = item.get("browser_account")
            if target is not None and (
                target not in by_key
                or by_key[target].get("browser_account")
                or by_key[target]["provider"] == item["provider"]
            ):
                raise ValueError("invalid shared browser account")
        if accounts.get("active") not in seen and not (not items and accounts.get("active") == ""):
            raise ValueError("active account does not exist")
        selected = accounts.get("selected", {})
        if not isinstance(selected, dict) or any(
            key not in seen or not key.startswith(provider + "/")
            for provider, key in selected.items()
        ):
            raise ValueError("invalid per-provider account selection")
        for key in ("language", "effort", "download_directory"):
            if not isinstance(data["application"].get(key), str):
                raise ValueError(f"invalid application setting {key}")
        if (
            not isinstance(data["application"].get("model", ""), str)
            or type(data["application"].get("mouse", True)) is not bool
            or type(data["application"].get("show_reasoning", True)) is not bool
        ):
            raise ValueError("invalid model/mouse preference")
        if (
            not data["application"]["download_directory"].strip()
            or "\0" in data["application"]["download_directory"]
        ):
            raise ValueError("İndirme klasörü boş veya geçersiz")
        if (
            type(data["application"]["download_allow_private"]) is not bool
            or type(data["application"]["download_max_bytes"]) is not int
            or data["application"]["download_max_bytes"] < 1
        ):
            raise ValueError("invalid download limits")
        if not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in data["keybindings"].items()
        ):
            raise ValueError("keybindings must map action IDs to key strings")
        keybinding_conflicts(data["keybindings"])
        json.dumps(data, allow_nan=False)

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def configure_provider(self, provider_id: str, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        with self._lock:
            data = self.snapshot()
            if provider_id not in data["providers"]:
                raise ValueError("unknown provider")
            data["providers"][provider_id]["enabled"] = enabled
            candidates = [
                item["provider"] + "/" + item["id"]
                for item in data["accounts"]["items"]
                if data["providers"].get(item["provider"], {}).get("enabled", False)
            ]
            if not candidates and data["accounts"]["items"]:
                raise ValueError("En az bir sağlayıcı ve ona ait bir hesap etkin kalmalı")
            if data["accounts"]["active"] not in candidates:
                chosen = data["accounts"].get("selected", {})
                data["accounts"]["active"] = next(
                    (key for key in chosen.values() if key in candidates),
                    candidates[0] if candidates else "",
                )
            self._commit(data)
        self._notify("providers", {provider_id: {"enabled": enabled}})

    def rename_account(self, key: str, label: str) -> None:
        """Change display metadata without replacing identity, sessions or preferences."""
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80:
            raise ValueError("Hesap adı 1–80 karakter olmalı")
        with self._lock:
            data = self.snapshot()
            item = next(
                (
                    item
                    for item in data["accounts"]["items"]
                    if item["provider"] + "/" + item["id"] == key
                ),
                None,
            )
            if item is None:
                raise ValueError("Hesap bulunamadı")
            item["label"] = label.strip()
            self._commit(data)
        self._notify("accounts", {"key": key, "label": label.strip()})

    def set_account_enabled(self, key: str, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        with self._lock:
            data = self.snapshot()
            item = next(
                (
                    item
                    for item in data["accounts"]["items"]
                    if item["provider"] + "/" + item["id"] == key
                ),
                None,
            )
            if item is None:
                raise ValueError("Hesap bulunamadı")
            item["enabled"] = enabled
            self._commit(data)
        self._notify("accounts", {"key": key, "enabled": enabled})

    def remove_account(self, key: str) -> str:
        """Remove metadata atomically, choosing another enabled account."""
        with self._lock:
            data = self.snapshot()
            accounts = data["accounts"]
            if not any(x["provider"] + "/" + x["id"] == key for x in accounts["items"]):
                raise ValueError("Hesap bulunamadı")
            remaining = [x for x in accounts["items"] if x["provider"] + "/" + x["id"] != key]
            candidates = [
                x["provider"] + "/" + x["id"]
                for x in remaining
                if data["providers"].get(x["provider"], {}).get("enabled", False)
            ]
            if not candidates:
                raise ValueError("Son etkin hesap silinemez; önce başka bir hesap ekleyin")
            accounts["items"] = remaining
            for item in remaining:
                if item.get("browser_account") == key:
                    item.pop("browser_account")
            accounts["selected"] = {
                provider: value
                for provider, value in accounts.get("selected", {}).items()
                if value != key
            }
            accounts.get("preferences", {}).pop(key, None)
            if accounts["active"] == key:
                accounts["active"] = candidates[0]
            self._commit(data)
        self._notify("accounts", accounts)
        return accounts["active"]

    def update(self, section: str, values: dict) -> None:
        if section not in ("application", "keybindings", "providers", "accounts"):
            raise ValueError(f"unknown settings section: {section}")
        with self._lock:
            if section == "keybindings" and any(
                key in values and values[key] != self._data["keybindings"].get(key)
                for key in IMMUTABLE_KEYBINDINGS
            ):
                raise ValueError("system keybindings cannot be modified")
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
        atomic_json_write(self.path, candidate, indent=2, allow_nan=False, prefix=".config-")
        self._data = copy.deepcopy(candidate)

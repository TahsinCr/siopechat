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

from providers.registry import PROVIDERS
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
    "providers": {key: {"enabled": True} for key in PROVIDERS},
    "extensions": {"layout": {"enabled": True}, "archive": {"enabled": True}},
    "accounts": {
        "active": "chatgpt/default",
        "selected": {},
        "preferences": {},
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
            if isinstance(value.get("providers"), dict):
                value["providers"].pop("default", None)  # Migrate the former selector.
            self._data = _merge(DEFAULTS, value)
        self._validate_document(self._data)

    @staticmethod
    def _validate_document(data: dict) -> None:
        if type(data.get("version")) is not int or data["version"] != 1:
            raise ValueError("unsupported settings version")
        for section in ("application", "keybindings", "providers", "extensions", "accounts"):
            if not isinstance(data.get(section), dict):
                raise ValueError(f"settings section {section} must be an object")
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
        if not isinstance(items, list) or not items:
            raise ValueError("accounts must contain at least one account")
        seen = set()
        for item in items:
            if (
                not isinstance(item, dict)
                or item.get("provider") not in PROVIDERS
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
        by_key = {x["provider"] + "/" + x["id"]: x for x in items}
        for item in items:
            target = item.get("browser_account")
            if target is not None and (
                target not in by_key
                or by_key[target].get("browser_account")
                or by_key[target]["provider"] == item["provider"]
            ):
                raise ValueError("invalid shared browser account")
        if accounts.get("active") not in seen:
            raise ValueError("active account does not exist")
        selected = accounts.get("selected", {})
        if not isinstance(selected, dict) or any(
            key not in seen or not key.startswith(provider + "/")
            for provider, key in selected.items()
        ):
            raise ValueError("invalid per-provider account selection")
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

    def configure_provider(self, provider_id, enabled):
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
            if not candidates:
                raise ValueError("En az bir sağlayıcı ve ona ait bir hesap etkin kalmalı")
            if data["accounts"]["active"] not in candidates:
                chosen = data["accounts"].get("selected", {})
                data["accounts"]["active"] = next(
                    (key for key in chosen.values() if key in candidates), candidates[0]
                )
            self._commit(data)
        self._notify("providers", {provider_id: {"enabled": enabled}})

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


class SettingsCatalog:
    def __init__(self, api, *, title=lambda key: key, order=lambda key: key):
        self.title, self.order = title, order
        self.api = api
        self.settings = api.settings
        self.languages = api.languages

    def rows(self) -> list[dict]:
        config = self.settings.snapshot()
        locale = config["application"]["language"]
        rows = [
            {
                "section": "application",
                "key": key,
                "title": self.languages.translate(f"application.{key}", locale),
                "value": value,
            }
            for key, value in config["application"].items()
            if key not in ("model", "effort")
        ]
        rows.extend(
            {
                "section": "keybindings",
                "key": key,
                "title": self.title(key),
                "value": value,
            }
            for key, value in sorted(
                config["keybindings"].items(), key=lambda item: self.order(item[0])
            )
        )
        for row in rows:
            if row["key"] == "language":
                row["choices"] = self.languages.locales()
            elif row["key"] == "theme":
                row["choices"] = self.api.themes.available()
        rows.extend(
            {
                "section": f"providers.{provider_id}",
                "key": "enabled",
                "title": provider_id.title() if provider_id != "chatgpt" else "ChatGPT",
                "value": config["providers"].get(provider_id, {}).get("enabled", True),
            }
            for provider_id in self.api.providers.ids()
        )
        schemas = self.settings.fields()
        for owner in dict.fromkeys(
            (*config["extensions"], *schemas, *self.api.get("extensions").statuses())
        ):
            fields = schemas.get(owner, ())
            values = self.settings.extension(owner)
            if not any(field.key == "enabled" for field in fields):
                rows.append(
                    {
                        "section": f"extensions.{owner}",
                        "key": "enabled",
                        "title": owner,
                        "value": values.get("enabled", True),
                    }
                )
            rows.extend(
                {
                    "section": f"extensions.{owner}",
                    "key": field.key,
                    "title": self.languages.translate(field.title_key, locale),
                    "value": values[field.key],
                    "choices": field.choices,
                }
                for field in fields
            )
        return rows

    async def change(self, section: str, key: str, value) -> None:
        row = next(
            (row for row in self.rows() if row["section"] == section and row["key"] == key), None
        )
        if row is None:
            raise ValueError("unknown setting")
        if row.get("choices") and value not in row["choices"]:
            raise ValueError("choose one of the available setting values")
        if section.startswith("providers."):
            self.api.get("accounts").set_enabled(section.split(".", 1)[1], value)
        elif section.startswith("extensions."):
            owner = section.removeprefix("extensions.")
            if key == "enabled":
                if type(value) is not bool:
                    raise ValueError("enabled must be true or false")
                manager = self.api.get("extensions")
                manager.enable(owner) if value else await manager.disable_async(owner)
            else:
                self.settings.update_extension(owner, {key: value})
        else:
            self.settings.update(section, {key: value})

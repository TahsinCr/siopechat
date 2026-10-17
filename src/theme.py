"""Shared semantic themes with optional frontend-specific style contributions."""

import copy
import importlib.util
import json
import re
from pathlib import Path


class ThemeService:
    DEFAULT = {
        "background": "#202020",
        "foreground": "#dedede",
        "accent": "#c3a6ff",
        "error": "#ff6666",
        "surface": "#252525",
        "muted": "#939393",
        "border": "#505050",
        "model": "#f0ad66",
        "effort": "#86d49b",
        "project": "#75b5ef",
        "user_background": "#303030",
        "assistant_background": "#181818",
        "reasoning_background": "#1c2732",
        "user_marker": "#86d49b",
        "assistant_marker": "#f0ad66",
        "reasoning_marker": "#75b5ef",
    }
    OPTION_RANGES = {
        "sidebar_width": (16, 40),
        "message_width": (40, 160),
        "composer_max_lines": (3, 16),
        "tab_width": (12, 40),
    }

    def __init__(self):
        self._themes = {}
        self.failures = {}

    def register(self, owner, theme_id, tokens, *, styles=None, markdown=None, options=None):
        if not theme_id or theme_id == "default" or theme_id in self._themes:
            raise ValueError("invalid or duplicate theme ID")
        if not isinstance(tokens, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", v)
            for k, v in tokens.items()
        ):
            raise ValueError("theme tokens must be six-digit hexadecimal colors")
        if any(
            value is not None and not isinstance(value, dict)
            for value in (styles, markdown, options)
        ):
            raise ValueError("theme style/options sections must be objects")
        for mapping in (styles or {}, markdown or {}):
            if not isinstance(mapping, dict) or not all(
                isinstance(k, str)
                and isinstance(v, str)
                and len(v) <= 256
                and not any(ord(c) < 32 for c in k + v)
                for k, v in mapping.items()
            ):
                raise ValueError("invalid frontend style mapping")
        for key, value in (options or {}).items():
            if (
                key not in self.OPTION_RANGES
                or type(value) is not int
                or not self.OPTION_RANGES[key][0] <= value <= self.OPTION_RANGES[key][1]
            ):
                raise ValueError("invalid theme layout option")
        self._themes[theme_id] = (
            owner,
            copy.deepcopy(
                {
                    "tokens": tokens,
                    "styles": styles or {},
                    "markdown": markdown or {},
                    "options": options or {},
                }
            ),
        )

    def unregister(self, owner):
        self._themes = {key: value for key, value in self._themes.items() if value[0] != owner}

    def resolve(self, theme_id):
        value = self._themes.get(theme_id, ("", {"tokens": {}}))[1]
        return copy.deepcopy(
            self.DEFAULT
            | value["tokens"]
            | {key: value.get(key, {}) for key in ("styles", "markdown", "options")}
        )

    def available(self):
        return ("default", *self._themes)

    def load_directory(self, directory: Path):
        for path in sorted(Path(directory).glob("*")):
            if path.suffix not in (".json", ".py") or path.name.startswith("_"):
                continue
            try:
                if path.suffix == ".json":
                    value = json.loads(path.read_text(encoding="utf-8"))
                else:
                    spec = importlib.util.spec_from_file_location("siope_theme_" + path.stem, path)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    value = module.THEME
                self.register(
                    f"theme:{path.stem}",
                    value["id"],
                    value.get("tokens", {}),
                    styles=value.get("styles"),
                    markdown=value.get("markdown"),
                    options=value.get("options"),
                )
            except Exception:
                self.failures[path.name] = "invalid_theme"

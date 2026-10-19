"""Namespaced extension translations, merged with locale and English fallback."""

import json
from pathlib import Path


class Catalog:
    def __init__(self, fallback: str = "en"):
        self.fallback = fallback
        self._bundles: dict[tuple[str, str], dict[str, str]] = {}

    def register(self, owner: str, locale: str, messages: dict[str, str]) -> None:
        if not owner or not locale or (owner, locale) in self._bundles:
            raise ValueError("duplicate or invalid language bundle")
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in messages.items()):
            raise ValueError("language bundles must contain string keys and values")
        self._bundles[owner, locale] = dict(messages)

    def load(self, owner: str, locale: str, path: Path) -> None:
        messages = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(messages, dict):
            raise ValueError("language bundle must be an object")
        self.register(owner, locale, messages)

    def unregister(self, owner: str) -> None:
        self._bundles = {key: value for key, value in self._bundles.items() if key[0] != owner}

    def locales(self) -> tuple[str, ...]:
        return tuple(sorted({locale for _, locale in self._bundles}))

    def merged(self, locale: str) -> dict[str, str]:
        result = {}
        for language in dict.fromkeys((self.fallback, locale.split("-")[0], locale)):
            for (owner, candidate), messages in self._bundles.items():
                if candidate == language:
                    result.update({f"{owner}.{key}": value for key, value in messages.items()})
        return result

    def translate(self, key: str, locale: str) -> str:
        return self.merged(locale).get(key, key)


def literal(messages: dict[str, str], source: str) -> str:
    """Translate a UI source string while retaining its layout whitespace."""
    key = source.strip()
    if not key:
        return source
    start = len(source) - len(source.lstrip())
    end = len(source.rstrip())
    return source[:start] + messages.get("application.ui." + key, key) + source[end:]

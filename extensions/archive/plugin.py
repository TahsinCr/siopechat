"""Bundled local archive extension, usable without any frontend."""

from extension import ExtensionSpec, SettingField

from .repository import Files, SQLiteRepository
from .sync import Archive


def activate(context):
    context.settings(SettingField("enabled", bool, True, "archive.enabled"))
    context.translations("en", {"enabled": "Local archive and synchronization"})
    context.translations("tr", {"enabled": "Yerel arşiv ve senkronizasyon"})
    root = context.api.get("paths") / "data"
    service = Archive(
        SQLiteRepository(root / "archive.sqlite3"), Files(root / "files"), context.api.events
    )
    context.provide("archive", service)


SPEC = ExtensionSpec("archive", activate)

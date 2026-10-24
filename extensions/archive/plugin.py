"""Bundled local archive extension, usable without any frontend."""

from extension import Action, ExtensionSpec, SettingField

from .repository import Files, SQLiteRepository
from .sync import Archive


def activate(context):
    context.settings(SettingField("enabled", bool, True, "archive.enabled"))
    context.translations("en", {"enabled": "Local archive and synchronization"})
    context.translations("tr", {"enabled": "Yerel arşiv ve senkronizasyon"})
    context.translations("ru", {"enabled": "Локальный архив и синхронизация"})
    root = context.api.get("paths") / "data"
    service = Archive(
        SQLiteRepository(root / "archive.sqlite3"), Files(root / "files"), context.api.events
    )
    context.provide("archive", service)
    context.action(
        Action("archive.collect", "Preview unreferenced archive files", service.collect_files)
    )
    context.action(
        Action("archive.inspect", "Inspect archive file references", service.inspect_files)
    )


SPEC = ExtensionSpec("archive", activate, api_version=3)

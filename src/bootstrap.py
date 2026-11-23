"""Application composition root for backend and CLI startup (zero TUI coupling)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


def _select_environment() -> None:
    root = Path(__file__).resolve().parent.parent
    candidate = root / "python" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if (
        sys.prefix == sys.base_prefix
        and candidate.is_file()
        and not os.environ.get("SIOPECHAT_BOOTSTRAPPED")
    ):
        environment = os.environ.copy()
        environment["SIOPECHAT_BOOTSTRAPPED"] = "1"
        os.execve(
            str(candidate),
            [str(candidate), str(Path(__file__).resolve()), *sys.argv[1:]],
            environment,
        )


class Application:
    """Central backend container owning runtime services, settings, archive, accounts, downloads."""

    def __init__(self, settings, runtime, actions) -> None:
        self.runtime = runtime
        self.settings = settings
        self.actions = actions
        settings.events = runtime.events

    @property
    def events(self):
        return self.runtime.events

    def provide(self, owner: str, name: str, service: Any) -> None:
        self.runtime.provide(owner, name, service)

    def get(self, name: str) -> Any:
        return self.runtime.get(name)

    @property
    def archive(self):
        return self.get("archive")

    @property
    def accounts(self):
        return self.get("accounts")

    @property
    def downloads(self):
        return self.get("downloads")


def create_application(root: Path | None = None) -> Application:
    from accounts import Accounts
    from actions import Action, ActionService
    from archive import Archive, Files, SQLiteRepository
    from downloads import Downloads
    from runtime import Runtime
    from settings import IMMUTABLE_KEYBINDINGS, SUPPORTED_LANGUAGES, Settings
    from utils.paths import (
        cache_dir,
        config_path,
        data_dir,
        downloads_dir,
        ensure_private_dir,
        portable_mode,
        project_root,
        runtime_dir,
        state_dir,
    )

    explicit_root = root is not None
    root = Path(root).expanduser().resolve() if explicit_root else project_root()
    storage_root = root if explicit_root or portable_mode() else None
    data_root = data_dir(storage_root)
    state_root = state_dir(storage_root)
    downloads_root = downloads_dir(storage_root)
    cache_root = cache_dir(storage_root)
    runtime_root = runtime_dir(storage_root)
    settings_path = config_path(storage_root)

    for directory in (settings_path.parent, data_root, state_root, cache_root):
        ensure_private_dir(directory)

    runtime = Runtime()
    settings = Settings(settings_path, events=runtime.events)
    actions = ActionService(runtime.events)
    app = Application(settings, runtime, actions)

    for name, value in (
        ("settings", settings),
        ("actions", actions),
        ("data_root", data_root),
        ("state_root", state_root),
        ("downloads_root", downloads_root),
        ("cache_root", cache_root),
        ("runtime_root", runtime_root),
    ):
        app.provide("application", name, value)

    archive = Archive(
        SQLiteRepository(data_root / "archive.sqlite3"),
        Files(data_root / "files"),
        runtime.events,
    )
    app.provide("application", "archive", archive)

    accounts = Accounts(app)
    app.provide("application", "accounts", accounts)

    downloads = Downloads(settings, root, accounts, default_directory=downloads_root)
    app.provide("application", "downloads", downloads)

    def change_setting(section: str, key: str, value: Any) -> None:
        if section == "keybindings" and key in IMMUTABLE_KEYBINDINGS:
            raise ValueError("system keybindings cannot be modified")
        if section == "application" and key == "language" and value not in SUPPORTED_LANGUAGES:
            raise ValueError("choose one of the available setting values")
        if section.startswith("providers."):
            if key != "enabled":
                raise ValueError("unknown setting")
            accounts.set_enabled(section.split(".", 1)[1], value)
        else:
            settings.update(section, {key: value})

    for action in (
        Action("accounts.list", "Hesapları listele", accounts.items),
        Action("accounts.add", "Hesap ekle", accounts.add),
        Action("accounts.select", "Hesap seç", accounts.select),
        Action("accounts.synchronize", "Verileri eşitle", accounts.synchronize),
        Action("archive.collect", "Preview unreferenced archive files", archive.collect_files),
        Action("archive.inspect", "Inspect archive file references", archive.inspect_files),
        Action("settings.change", "Change setting", change_setting),
    ):
        actions.register("application", action)

    if not settings.path.exists():
        settings.save()

    runtime.on_shutdown(accounts.close)
    return app


def main(argv: list[str] | None = None) -> int:
    _select_environment()
    try:
        app = create_application()
    except (ImportError, OSError, ValueError) as error:
        print(f"siopechat: {error}", file=sys.stderr)
        return 1
    from cli import main as cli_main

    return cli_main(app, argv)


if __name__ == "__main__":
    raise SystemExit(main())

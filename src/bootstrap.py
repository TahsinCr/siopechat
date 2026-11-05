"""Common startup and application composition for every operating system."""

from __future__ import annotations

import os
import sys
from pathlib import Path


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


def create_application(root: Path | None = None):
    checkout = Path(__file__).resolve().parent.parent
    if (checkout / "extensions" / "__init__.py").is_file() and str(checkout) not in sys.path:
        sys.path.insert(0, str(checkout))
    from webai_provider import create_provider

    from accounts import Accounts
    from actions import Action
    from backend import BackendService
    from extension import ExtensionAPI, ExtensionManager, View
    from extensions import __file__ as extensions_file
    from lang import __file__ as language_file
    from settings import Settings, SettingsCatalog
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
    settings = Settings(settings_path)
    api = ExtensionAPI(settings)
    api.provide("application", "data_root", data_root)
    api.provide("application", "state_root", state_root)
    api.provide("application", "downloads_root", downloads_root)
    api.provide("application", "cache_root", cache_root)
    api.provide("application", "runtime_root", runtime_root)
    from accounts.repository import AccountRepository

    repository = AccountRepository(data_root / "accounts.sqlite3")
    backend = BackendService(
        root, data_root=data_root, runtime_root=runtime_root, repository=repository
    )
    api.provide("application", "backend", backend)
    api.providers.register(
        create_provider("chatgpt", session=backend.create_session()),
        account_factory=lambda account_id, **config: create_provider(
            "chatgpt", account_id, **config
        ),
    )

    api.providers.register(
        create_provider("gemini", credential_file=repository.credentials("gemini/default")),
        account_factory=lambda account_id, **config: create_provider(
            "gemini", account_id, **config
        ),
    )
    api.providers.register(
        create_provider("claude", credential_file=repository.credentials("claude/default")),
        account_factory=lambda account_id, **config: create_provider(
            "claude", account_id, **config
        ),
    )
    api.providers.register(
        create_provider("grok", credential_file=repository.credentials("grok/default")),
        account_factory=lambda account_id, **config: create_provider("grok", account_id, **config),
    )
    accounts = Accounts(api)
    api.provide("application", "accounts", accounts)
    api.tasks.provider_resolver = lambda provider, account: accounts.provider(
        provider + "/" + account
    )
    from downloads import Downloads

    api.provide(
        "application",
        "downloads",
        Downloads(settings, root, accounts, default_directory=downloads_root),
    )
    api.actions.register(
        "application", Action("accounts.list", "Hesapları listele", accounts.items)
    )
    api.actions.register("application", Action("accounts.add", "Hesap ekle", accounts.add))
    api.actions.register("application", Action("accounts.select", "Hesap seç", accounts.select))
    api.actions.register(
        "application", Action("accounts.synchronize", "Verileri eşitle", accounts.synchronize)
    )
    for bundle in sorted(Path(language_file).parent.glob("*.json")):
        api.languages.load("application", bundle.stem, bundle)
    panel = SettingsCatalog(api)
    api.views.register("application", View("settings", "Settings", lambda: panel))
    api.actions.register("application", Action("settings.show", "Show settings", panel.rows))
    manager = ExtensionManager(api)
    api.provide("application", "extensions", manager)
    api.actions.register(
        "application", Action("extensions.enable", "Enable extension", manager.enable)
    )
    api.actions.register(
        "application", Action("extensions.disable", "Disable extension", manager.disable_async)
    )
    api.actions.register("application", Action("settings.change", "Change setting", panel.change))
    bundled_extensions = Path(extensions_file).parent
    manager.load_directory(bundled_extensions)
    if (root / "extensions").resolve() != bundled_extensions.resolve():
        manager.load_directory(root / "extensions")
    for directory in (settings_path.parent, data_root, state_root, cache_root):
        ensure_private_dir(directory)
    if not settings.path.exists():
        settings.save()
    # Stop provider work and finish accepted-response writes before extensions
    # close their repositories. Runtime shutdown callbacks execute in reverse.
    api.runtime.on_shutdown(lambda: accounts.close())
    return api


def main(argv: list[str] | None = None) -> int:
    _select_environment()
    try:
        api = create_application()
    except (ImportError, OSError, ValueError) as error:
        print(f"siopechat: {error}", file=sys.stderr)
        return 1
    from cli import main as cli_main

    return cli_main(api, argv)


if __name__ == "__main__":
    raise SystemExit(main())

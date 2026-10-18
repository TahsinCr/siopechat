"""Common startup and application composition for every operating system."""

from __future__ import annotations

import asyncio
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
    from accounts import Accounts
    from actions import Action
    from backend import BackendService
    from extension import ExtensionAPI, ExtensionManager
    from extensions.layout.plugin import SPEC
    from lang import __file__ as language_file
    from providers.chatgpt import ChatGPTProvider
    from settings import Settings, SettingsCatalog
    from utils.paths import project_root
    from views import View

    root = Path(root) if root is not None else project_root()
    settings = Settings(root / "config.json")
    api = ExtensionAPI(settings)
    api.provide("application", "paths", root)
    backend = BackendService(root)
    api.provide("application", "backend", backend)
    api.providers.register(ChatGPTProvider(backend.create_session()))
    from providers.gemini import GeminiProvider

    api.providers.register(
        GeminiProvider(root / "data" / "accounts" / "gemini" / "default" / "cookies.json")
    )
    accounts = Accounts(api)
    api.provide("application", "accounts", accounts)
    api.actions.register(
        "application", Action("accounts.list", "Hesapları listele", accounts.items)
    )
    api.actions.register("application", Action("accounts.add", "Hesap ekle", accounts.add))
    api.actions.register("application", Action("accounts.select", "Hesap seç", accounts.select))
    for locale in ("en", "tr"):
        api.languages.load("application", locale, Path(language_file).parent / f"{locale}.json")
    from themes import __file__ as themes_file

    api.themes.load_directory(Path(themes_file).parent)
    custom_themes = root / "themes"
    if custom_themes.resolve() != Path(themes_file).parent.resolve():
        api.themes.load_directory(custom_themes)
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
    manager.register(SPEC)
    manager.activate("layout")
    manager.load_directory(checkout / "extensions")
    if (root / "extensions").resolve() != (checkout / "extensions").resolve():
        manager.load_directory(root / "extensions")
    (root / "data").mkdir(parents=True, exist_ok=True)
    if not settings.path.exists():
        settings.save()
    # Stop provider work and finish accepted-response writes before extensions
    # close their repositories. Runtime shutdown callbacks execute in reverse.
    api.runtime.on_shutdown(lambda: asyncio.to_thread(accounts.close))
    return api


def main(argv: list[str] | None = None) -> int:
    _select_environment()
    try:
        api = create_application()
    except (ImportError, OSError, ValueError) as error:
        print(f"siopechat: {error}", file=sys.stderr)
        return 1
    from cli.entrypoint import main as cli_main

    return cli_main(api, argv)


if __name__ == "__main__":
    raise SystemExit(main())

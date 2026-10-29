"""Compose the terminal UI with application services."""

import asyncio
from pathlib import Path
from typing import Any

from providers.chatgpt.constants import DEFAULT_CDP_URL


def run_fullscreen(
    api,
    socket_path: Path,
    conversation_id: str | None = None,
    cached_conversation: dict[str, Any] | None = None,
    cdp_url: str = DEFAULT_CDP_URL,
) -> int:
    from accounts import identity, origin

    from .chat import run_tui
    from .settings import SettingsPanel
    from .tab_session import TabSessionStore

    accounts = api.get("accounts")
    accounts.cdp_url = cdp_url
    key = accounts.active()
    if key != "chatgpt/default":
        cached_conversation, conversation_id = None, None
    session_store = TabSessionStore(Path(api.get("paths")) / "data" / "session.json")
    initial_session = (
        session_store.load() if conversation_id is None and cached_conversation is None else None
    )
    cached = {
        name: value
        for name, value in (cached_conversation or {}).items()
        if name not in ("messages", "metadata")
    }
    cached_conversation = None
    if not conversation_id and initial_session is None:
        candidate = accounts.active_conversation(key)
        conversation_id = candidate if isinstance(candidate, str) else cached.get("id")

    def preferences():
        config = api.settings.snapshot()
        return {
            "settings": config,
            "translations": api.languages.merged(config["application"]["language"]),
        }

    def change_setting(section, name, value):
        asyncio.run(api.actions.invoke("settings.change", section=section, key=name, value=value))
        return preferences()

    def layout_factory(view):
        if api.settings.extension("layout").get("enabled", True):
            return api.get("workspaces").create_layout(view)
        return None

    def action_host(view):
        from actions import Action

        from .actions import action_title

        api.actions.unregister("terminal")
        existing = {action.id for action in api.actions.catalog()}
        locale = api.settings.snapshot()["application"]["language"]
        messages = api.languages.merged(locale)
        for action_id in view.action_handlers:
            if action_id not in existing:
                api.actions.register(
                    "terminal",
                    Action(
                        action_id,
                        action_title(action_id, messages),
                        lambda action_id=action_id: view._dispatch_ui(
                            lambda: view.dispatch_action(action_id)
                        ),
                        view.keybindings.get(action_id),
                    ),
                )
        return lambda: api.actions.unregister("terminal")

    callbacks = accounts.callbacks(key)
    run_tui(
        lambda: accounts.list_chats()["items"],
        callbacks["load_conversation"],
        callbacks["send_prompt"],
        conversation_id=identity(key, origin(conversation_id or cached.get("id"), key)[1]),
        title=cached.get("title") or "Yeni sohbet",
        initial_messages=None,
        model=cached.get("model"),
        account_service=accounts,
        action_host=action_host,
        layout_factory=layout_factory,
        initial_session=initial_session,
        session_store=session_store,
        keybindings=api.settings.snapshot()["keybindings"],
        settings_rows=SettingsPanel(api).rows,
        change_setting=change_setting,
        load_preferences=preferences,
        list_workspaces=lambda: (
            asyncio.run(api.actions.invoke("layout.list"))
            if api.settings.extension("layout").get("enabled", True)
            else []
        ),
        list_actions=lambda: [
            {
                "id": action.id,
                "title": action.title,
                "detail": action.keybinding or "",
                "keybinding": action.keybinding or "",
                "parameters": action.parameters,
            }
            for action in api.actions.search()
        ],
        list_shortcut_actions=lambda: [
            {
                "id": action.id,
                "keybinding": action.keybinding or "",
                "available": lambda action_id=action.id: api.actions.is_available(action_id),
            }
            for action in api.actions.catalog()
        ],
        invoke_action=lambda action_id, arguments=None: asyncio.run(
            api.actions.invoke(action_id, **(arguments or {}))
        ),
    )
    return 0

"""Compose the terminal UI with application services."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from webaiprovider.providers.chatgpt.constants import DEFAULT_CDP_URL


def register_workspace_actions(api, workspaces) -> None:
    from actions import Action

    for command in ("list", "create", "show", "delete"):
        action_id = f"layout.{command}"
        if any(item.id == action_id for item in api.actions.catalog()):
            continue
        parameters: tuple[dict, ...] = ()
        if command == "create":
            parameters = ({"key": "name", "label": "Çalışma alanı adı", "required": True},)
        elif command in ("show", "delete"):
            parameters = (
                {"key": "workspace_id", "label": "Çalışma alanı kimliği", "required": True},
            )
        api.actions.register(
            "tui",
            Action(
                action_id,
                f"Workspace: {command}",
                lambda cmd=command, **kwargs: workspaces.execute_sync(cmd, **kwargs),
                parameters=parameters,
            ),
        )


def run_fullscreen(
    api,
    socket_path: Path | None = None,
    conversation_id: str | None = None,
    cached_conversation: dict[str, Any] | None = None,
    cdp_url: str = DEFAULT_CDP_URL,
) -> int:
    del socket_path
    from accounts import identity, origin

    from .actions import action_title
    from .chat import run_tui
    from .lang import load_catalog
    from .settings import SettingsPanel
    from .tab_session import TabSessionStore
    from .workspaces import Workspaces

    accounts = api.get("accounts")
    accounts.cdp_url = cdp_url
    key = accounts.active()
    if key != "chatgpt/default":
        cached_conversation, conversation_id = None, None
    session_store = TabSessionStore(api.get("state_root") / "session.json")
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

    languages = load_catalog()
    workspaces = Workspaces(api.get("data_root") / "workspaces.sqlite3")
    register_workspace_actions(api, workspaces)

    def preferences():
        config = api.settings.snapshot()
        return {
            "settings": config,
            "translations": languages.merged(config["application"]["language"]),
        }

    def change_setting(section, name, value):
        api.actions.invoke_sync("settings.change", section=section, key=name, value=value)
        return preferences()

    def layout_factory(view):
        return workspaces.create_layout(view)

    def action_host(view):
        from actions import Action

        api.actions.unregister("terminal")
        existing = {action.id for action in api.actions.catalog()}
        locale = api.settings.snapshot()["application"]["language"]
        messages = languages.merged(locale)
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
    settings_panel = SettingsPanel(api, languages=languages)
    run_tui(
        lambda: accounts.list_chats()["items"],
        callbacks["load_conversation"],
        callbacks["send_prompt"],
        conversation_id=identity(key, origin(conversation_id or cached.get("id"), key)[1]),
        title=cached.get("title") or "New conversation",
        initial_messages=None,
        model=cached.get("model"),
        upload_image=callbacks.get("upload_image"),
        rename_conversation=callbacks.get("rename_conversation"),
        load_catalog=callbacks.get("load_catalog"),
        list_projects=callbacks.get("list_projects_callback"),
        get_project=callbacks.get("get_project_callback"),
        list_project_chats=callbacks.get("list_project_chats_callback"),
        account_service=accounts,
        action_host=action_host,
        layout_factory=layout_factory,
        initial_session=initial_session,
        session_store=session_store,
        keybindings=api.settings.snapshot()["keybindings"],
        settings_rows=settings_panel.rows,
        change_setting=change_setting,
        load_preferences=preferences,
        list_workspaces=lambda: workspaces.execute_sync("list"),
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
        invoke_action=lambda action_id, arguments=None: api.actions.invoke_sync(
            action_id, **(arguments or {})
        ),
        move_conversation=callbacks.get("move_conversation"),
        session_action=callbacks.get("session_action"),
        forget_temporary=callbacks.get("forget_temporary"),
    )
    return 0

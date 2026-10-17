"""Read ChatGPT web conversations through a Chrome-authenticated HTTP client."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

import httpx

from providers.chatgpt.constants import DEFAULT_CDP_URL, DEFAULT_PAGE_SIZE
from providers.chatgpt.errors import ChatGPTWebError
from providers.chatgpt.formats import new_messages, normalize_conversation
from providers.errors import ProviderError


def print_conversation_list(items: list[dict[str, Any]], output: str) -> None:
    if output == "json":
        print(json.dumps(items, ensure_ascii=False, indent=2))
    elif output == "jsonl":
        for item in items:
            print(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
    else:
        for item in items:
            print(f"{item.get('id', '')}\t{item.get('title') or 'Untitled'}")


def print_conversation(conversation: dict[str, Any], output: str) -> None:
    normalized = normalize_conversation(conversation)
    if output == "json":
        print(json.dumps(normalized, ensure_ascii=False, indent=2))
    elif output == "jsonl":
        envelope = {key: value for key, value in normalized.items() if key != "messages"}
        envelope["type"] = "conversation"
        print(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
        for message in normalized["messages"]:
            print(
                json.dumps(
                    {"type": "message", **message}, ensure_ascii=False, separators=(",", ":")
                )
            )
    else:
        print(f"# {normalized.get('title') or 'Untitled'}")
        for message in normalized["messages"]:
            text = message.get("text", "")
            if not text:
                continue
            role = message.get("role") or "unknown"
            name = message.get("name")
            heading = f"{role} ({name})" if name else role
            print(f"\n## {heading}\n\n{text}")


def print_prompt_result(result: dict[str, Any], output: str) -> None:
    messages = new_messages(result.get("conversation", {}), set(result.get("previous_ids", [])))
    if output == "json":
        print(
            json.dumps(
                {"conversation_id": result.get("conversation_id"), "messages": messages},
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if output == "jsonl":
        for message in messages:
            print(
                json.dumps(
                    {"type": "message", **message}, ensure_ascii=False, separators=(",", ":")
                )
            )
        return
    for message in messages:
        if message.get("role") != "assistant" or not message.get("text"):
            continue
        print(message["text"])


def stream_prompt(
    api, socket_path: Path, conversation_id: str | None, prompt: str, output: str
) -> dict[str, Any]:
    rendered = ""

    def on_event(event: dict[str, Any]) -> None:
        nonlocal rendered
        event_type = event.get("type")
        if output == "jsonl" and event_type != "heartbeat":
            print(json.dumps(event, ensure_ascii=False, separators=(",", ":")), flush=True)
        if output != "text":
            return
        if event_type == "delta":
            text = event.get("text", "")
            if isinstance(text, str):
                print(text, end="", flush=True)
                rendered += text
        elif event_type == "replace":
            text = event.get("text", "")
            if isinstance(text, str):
                print(("\n" if rendered else "") + text, end="", flush=True)
                rendered = text

    result = api.get("backend").daemon_stream_request(
        socket_path,
        {"command": "send_stream", "conversation_id": conversation_id, "prompt": prompt},
        on_event,
    )
    if output == "text":
        messages = new_messages(result.get("conversation", {}), set(result.get("previous_ids", [])))
        final = "\n".join(
            (
                message.get("text", "")
                for message in messages
                if message.get("role") == "assistant" and message.get("text")
            )
        )
        if final.startswith(rendered):
            print(final[len(rendered) :], end="")
        elif not rendered:
            print(final, end="")
        elif final and final != rendered:
            print(f"\n{final}", end="")
        print()
    else:
        print_prompt_result(result, output)
    return result


def run_fullscreen(
    api,
    socket_path: Path,
    conversation_id: str | None = None,
    cached_conversation: dict[str, Any] | None = None,
    cdp_url: str = DEFAULT_CDP_URL,
) -> int:
    try:
        from .chat import run_tui
    except ImportError as error:
        raise ChatGPTWebError(
            "fullscreen UI dependencies are missing; install the SiopeChat project dependencies"
        ) from error

    session_enabled = True
    session_lock = threading.RLock()

    def ensure_service() -> None:
        with session_lock:
            if not session_enabled:
                raise ChatGPTWebError("Oturum kapalı · F12 ile giriş yapın")
            backend = api.get("backend")
            if (
                not backend.auth_store_path().is_file()
                and not backend.daemon_is_ready(socket_path)
                and not backend.create_session(cdp_url).is_ready()
            ):
                raise ChatGPTWebError("Kayıtlı oturum yok · F12 → Bağlı tarayıcı ile giriş yapın")
            backend.ensure_daemon(socket_path, cdp_url)

    def preferences():
        config = api.settings.snapshot()
        return {
            "settings": config,
            "translations": api.languages.merged(config["application"]["language"]),
            "tokens": api.themes.resolve(config["application"]["theme"]),
        }

    def change_setting(section, key, value):
        asyncio.run(api.actions.invoke("settings.change", section=section, key=key, value=value))
        return preferences()

    def session_action(action):
        nonlocal session_enabled
        backend = api.get("backend")
        session = backend.create_session(cdp_url)
        if action == "status":
            return {"saved_snapshot": backend.auth_store_path().is_file()}
        if action == "default_browser":
            return session.open_login()
        if action == "connect_browser":
            session_enabled = True
            return session.launch()
        if action == "verify":
            session_enabled = True
            ensure_service()
            return dict(
                backend.daemon_request(socket_path, {"command": "auth"}),
                saved_snapshot=backend.auth_store_path().is_file(),
            )
        if action == "logout":
            with session_lock:
                session_enabled = False
                try:
                    return backend.forget_session(socket_path)
                except BaseException:
                    session_enabled = True
                    raise
        raise ValueError("Unknown session action")

    def list_recent() -> list[dict[str, Any]]:
        ensure_service()
        items = (
            api.get("backend")
            .daemon_request(socket_path, {"command": "list", "maximum": 50})
            .get("items", [])
        )
        return items if isinstance(items, list) else []

    def provider_request(command: str, **arguments) -> dict[str, Any]:
        ensure_service()
        return api.get("backend").daemon_request(socket_path, {"command": command, **arguments})

    def load_catalog():
        from providers.models import ModelCatalog

        return ModelCatalog.from_dict(provider_request("models", refresh=True)["catalog"])

    def list_projects(cursor=None):
        return provider_request("projects", cursor=cursor)

    def get_project(project_id):
        return provider_request("project", project_id=project_id)["project"]

    def list_project_chats(project_id, cursor=None):
        return provider_request("project_chats", project_id=project_id, cursor=cursor)

    def move_conversation(conversation_id, project_id):
        return provider_request("move_chat", conversation_id=conversation_id, project_id=project_id)

    def load(selected_id: str) -> dict[str, Any]:
        ensure_service()
        raw = (
            api.get("backend")
            .daemon_request(
                socket_path, {"command": "show_normalized", "conversation_id": selected_id}
            )
            .get("conversation", {})
        )
        if isinstance(raw, dict):
            return raw
        return {}

    def send(
        selected_id: str | None,
        prompt: str,
        attachments: list[dict[str, Any]],
        on_event: Any,
        on_connection: Any,
        model: str | None = None,
        effort: str | None = None,
        **options,
    ) -> dict[str, Any]:
        ensure_service()
        result = api.get("backend").daemon_stream_request(
            socket_path,
            {
                "command": "send_stream",
                "conversation_id": selected_id,
                "prompt": prompt,
                "attachments": attachments,
                **({"model": model} if model is not None else {}),
                **({"effort": effort} if effort is not None else {}),
                **options,
            },
            on_event,
            on_connection,
        )
        return result

    def upload_image(
        data: bytes, file_name: str, mime_type: str, width: int, height: int
    ) -> dict[str, Any]:
        ensure_service()
        return (
            api.get("backend")
            .daemon_request(
                socket_path,
                {
                    "command": "upload_image",
                    "data": base64.b64encode(data).decode("ascii"),
                    "file_name": file_name,
                    "mime_type": mime_type,
                    "width": width,
                    "height": height,
                },
            )
            .get("attachment", {})
        )

    def rename(selected_id: str, new_title: str) -> None:
        ensure_service()
        api.get("backend").daemon_request(
            socket_path, {"command": "rename", "conversation_id": selected_id, "title": new_title}
        )

    try:
        application_settings = api.settings.snapshot()
    except (AttributeError, TypeError):
        application_settings = {}
    if not isinstance(application_settings, dict):
        application_settings = {}

    def list_workspaces() -> list[dict[str, str]]:
        if not any(action.id == "layout.list" for action in api.actions.search()):
            return []
        return asyncio.run(api.actions.invoke("layout.list"))

    def list_actions() -> list[dict[str, str]]:
        overrides = api.settings.snapshot().get("keybindings", {})
        return [
            {
                "id": action.id,
                "title": action.title,
                "detail": overrides.get(action.id) or action.keybinding or action.id,
            }
            for action in api.actions.search()
        ]

    def invoke_action(action_id: str, arguments: dict[str, Any] | None = None) -> Any:
        return asyncio.run(api.actions.invoke(action_id, **(arguments or {})))

    def forget_temporary(remote_id):
        backend = api.get("backend")
        if backend.daemon_is_ready(socket_path):
            return backend.daemon_request(
                socket_path, {"command": "forget_temporary", "conversation_id": remote_id}
            )
        return {"forgotten": False}

    cached_id = cached_conversation.get("id") if cached_conversation else None
    cached_messages = cached_conversation.get("messages") if cached_conversation else None
    initial_project = {}
    if cached_conversation:
        project = cached_conversation.get("project")
        if isinstance(project, dict):
            initial_project.update(project)
        project_id = cached_conversation.get("project_id")
        if isinstance(project_id, str) and project_id:
            initial_project["id"] = project_id
        if cached_conversation.get("project_name"):
            initial_project["name"] = cached_conversation["project_name"]
    if isinstance(cached_id, str) and cached_id:
        api.get("backend").set_active_conversation(cached_id)
    run_tui(
        list_recent,
        load,
        send,
        conversation_id=conversation_id or (cached_id if isinstance(cached_id, str) else None),
        title=cached_conversation.get("title") or "Untitled"
        if cached_conversation
        else "Loading conversation"
        if conversation_id
        else "New conversation",
        initial_messages=cached_messages if isinstance(cached_messages, list) else None,
        model=cached_conversation.get("model") if cached_conversation else None,
        upload_image=upload_image,
        rename_conversation=rename,
        set_active_conversation=api.get("backend").set_active_conversation,
        keybindings=application_settings.get("keybindings", {}),
        list_workspaces=list_workspaces,
        list_actions=list_actions,
        invoke_action=invoke_action,
        effort_preference=application_settings.get("application", {}).get("effort", "medium"),
        load_catalog=load_catalog,
        list_projects=list_projects,
        get_project=get_project,
        list_project_chats=list_project_chats,
        initial_project=initial_project,
        refresh_on_start=bool(cached_conversation and "project_id" not in cached_conversation),
        forget_temporary=forget_temporary,
        move_conversation=move_conversation,
        layout_factory=lambda view: (
            api.get("workspaces").create_layout(view)
            if api.settings.extension("layout").get("enabled", True)
            else None
        ),
        settings_rows=lambda: asyncio.run(api.actions.invoke("settings.show")),
        change_setting=change_setting,
        load_preferences=preferences,
        session_action=session_action,
        list_history_page=lambda cursor: provider_request(
            "history_page", offset=int(cursor or 0), limit=50
        ),
    )
    return 0


def build_parser(api) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="siopechat",
        epilog="Management: settings, extensions, providers, actions, action. Use siopechat actions to list registered actions.",
    )
    parser.add_argument(
        "--cdp-url",
        default=os.environ.get("CHATGPT_WEB_CDP_URL", DEFAULT_CDP_URL),
        help="Chrome DevTools HTTP endpoint (default: %(default)s)",
    )
    parser.add_argument(
        "--socket",
        type=Path,
        default=api.get("backend").default_socket_path(),
        help="daemon Unix socket (default: %(default)s)",
    )
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("workspace", help="manage saved local tab workspaces")
    commands.add_parser("login", help="launch the persistent login browser")
    auth_parser = commands.add_parser("auth", help="verify the saved ChatGPT session")
    auth_parser.add_argument(
        "--refresh", action="store_true", help="recapture credentials from Chrome"
    )
    list_parser = commands.add_parser("list", help="list conversations")
    extent = list_parser.add_mutually_exclusive_group()
    extent.add_argument("--all", action="store_true", help="fetch every page")
    extent.add_argument("--limit", type=int, default=DEFAULT_PAGE_SIZE)
    list_parser.add_argument("--output", choices=("table", "json", "jsonl"), default="table")
    show_parser = commands.add_parser("show", help="show one conversation")
    show_parser.add_argument("conversation_id")
    show_parser.add_argument("--output", choices=("markdown", "json", "jsonl"), default="markdown")
    rename_parser = commands.add_parser("rename", help="rename a conversation")
    rename_parser.add_argument("conversation_id")
    rename_parser.add_argument("title", nargs="+")
    send_parser = commands.add_parser("send", help="send a prompt")
    send_parser.add_argument("conversation_id")
    send_parser.add_argument("prompt", nargs="*")
    send_parser.add_argument("--output", choices=("text", "json", "jsonl"), default="text")
    new_parser = commands.add_parser("new", help="start a new conversation")
    new_parser.add_argument("prompt", nargs="*")
    new_parser.add_argument("--output", choices=("text", "json", "jsonl"), default="text")
    resume_parser = commands.add_parser("resume", help="open an interactive conversation")
    resume_parser.add_argument("conversation_id")
    daemon_parser = commands.add_parser(
        "daemon", help="run the background service in the foreground"
    )
    daemon_parser.add_argument("--socket", dest="daemon_socket", type=Path)
    commands.add_parser("status", help="show daemon, session, and browser state")
    commands.add_parser("stop", help="stop the background daemon")
    commands.add_parser("logout", help="stop the daemon and delete saved credentials")
    return parser


def main(api, argv: list[str] | None = None) -> int:
    args = build_parser(api).parse_args(argv)
    try:
        if args.command == "login":
            print(
                json.dumps(
                    api.get("backend").create_session(args.cdp_url).launch(), separators=(",", ":")
                )
            )
            return 0
        if args.command == "daemon":
            return api.get("backend").run_daemon(args.daemon_socket or args.socket, args.cdp_url)
        if args.command == "stop":
            result = api.get("backend").daemon_request(args.socket, {"command": "stop"})
            print(json.dumps(result, separators=(",", ":")))
            return 0
        if args.command == "logout":
            result = api.get("backend").forget_session(args.socket)
            print(json.dumps(result, separators=(",", ":")))
            return 0
        if args.command is None:
            if not sys.stdin.isatty():
                raise ChatGPTWebError(
                    "interactive mode requires a terminal; use new or send for piped input"
                )
            cached = api.get("backend").load_cached_conversation()
            return run_fullscreen(
                api, args.socket, cached_conversation=cached, cdp_url=args.cdp_url
            )
        if args.command == "resume":
            cached = api.get("backend").load_cached_conversation(args.conversation_id)
            return run_fullscreen(
                api,
                args.socket,
                args.conversation_id,
                cached_conversation=cached,
                cdp_url=args.cdp_url,
            )
        api.get("backend").ensure_daemon(args.socket, args.cdp_url)
        if args.command == "auth":
            result = api.get("backend").daemon_request(
                args.socket, {"command": "auth", "refresh": args.refresh}
            )
            print(json.dumps(result, separators=(",", ":")))
        elif args.command == "status":
            result = api.get("backend").daemon_request(args.socket, {"command": "status"})
            print(json.dumps(result, separators=(",", ":")))
        elif args.command == "list":
            if args.limit is not None and args.limit < 1:
                raise ChatGPTWebError("--limit must be greater than zero")
            result = api.get("backend").daemon_request(
                args.socket, {"command": "list", "maximum": None if args.all else args.limit}
            )
            print_conversation_list(result.get("items", []), args.output)
        elif args.command == "show":
            result = api.get("backend").daemon_request(
                args.socket, {"command": "show", "conversation_id": args.conversation_id}
            )
            print_conversation(result.get("conversation", {}), args.output)
        elif args.command == "rename":
            result = api.get("backend").daemon_request(
                args.socket,
                {
                    "command": "rename",
                    "conversation_id": args.conversation_id,
                    "title": " ".join(args.title),
                },
            )
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        elif args.command in ("send", "new"):
            prompt = " ".join(args.prompt).strip()
            if not prompt and (not sys.stdin.isatty()):
                prompt = sys.stdin.read().strip()
            if not prompt:
                raise ChatGPTWebError("provide a prompt argument or pipe one on stdin")
            result = stream_prompt(
                api,
                args.socket,
                args.conversation_id if args.command == "send" else None,
                prompt,
                args.output,
            )
            if args.command == "new" and args.output == "text":
                print(f"\n[conversation {result.get('conversation_id')}]", file=sys.stderr)
    except KeyboardInterrupt:
        print("\n[interrupted]", file=sys.stderr)
        return 130
    except (ProviderError, httpx.HTTPError) as error:
        print(f"siopechat: {error}", file=sys.stderr)
        return 1
    return 0

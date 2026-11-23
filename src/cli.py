"""Application command-line interface and fullscreen TUI launcher."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from typing import Any

from webaiprovider import ChatGPTError, WebAIProviderError, WebAIProviderType
from webaiprovider.providers.chatgpt.constants import DEFAULT_CDP_URL, DEFAULT_PAGE_SIZE

from accounts.shared import PROVIDERS, provider_capabilities


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
    if output == "json":
        print(json.dumps(conversation, ensure_ascii=False, indent=2))
    elif output == "jsonl":
        envelope = {k: v for k, v in conversation.items() if k != "messages"}
        envelope["type"] = "conversation"
        print(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
        for message in conversation.get("messages") or ():
            print(
                json.dumps(
                    {"type": "message", **message}, ensure_ascii=False, separators=(",", ":")
                )
            )
    else:
        print(f"# {conversation.get('title') or 'Untitled'}")
        for message in conversation.get("messages") or ():
            text = message.get("text") or ""
            if not text:
                continue
            role = message.get("role") or "unknown"
            heading = role
            print(f"\n## {heading}\n\n{text}")


def stream_prompt(
    accounts,
    account_key: str,
    conversation_id: str | None,
    prompt: str,
    output: str,
) -> dict[str, Any]:
    rendered = ""

    def on_event(event: dict[str, Any]) -> None:
        nonlocal rendered
        event_type = event.get("type")
        if output == "jsonl":
            print(json.dumps(event, ensure_ascii=False, separators=(",", ":")), flush=True)
            return
        if output != "text" or event.get("channel") == "analysis":
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

    result = accounts.send(
        account_key,
        conversation_id,
        prompt,
        [],
        on_event,
        lambda _conn: None,
    )
    conversation = result.get("conversation") or {}
    parts = conversation.get("parts") or []
    if output == "text":
        final = "\n".join(
            part["text"] for part in parts if part.get("role") == "assistant" and part.get("text")
        )
        if final.startswith(rendered):
            print(final[len(rendered) :], end="")
        elif not rendered:
            print(final, end="")
        elif final and final != rendered:
            print(f"\n{final}", end="")
        print()
    elif output == "json":
        print(
            json.dumps(
                {
                    "conversation_id": result.get("conversation_id"),
                    "parts": parts,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    elif output == "jsonl":
        print(json.dumps({"type": "result", **result}, ensure_ascii=False, separators=(",", ":")))
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="siopechat",
        epilog="Management: workspace, settings, providers, actions, action. Use siopechat actions to list registered actions.",
    )
    parser.add_argument(
        "--cdp-url",
        default=os.environ.get("CHATGPT_WEB_CDP_URL", DEFAULT_CDP_URL),
        help="Chrome DevTools HTTP endpoint (default: %(default)s)",
    )
    parser.add_argument(
        "--account",
        default=None,
        help="Target account key (default: active account)",
    )
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("login", help="launch the persistent login browser")
    auth_parser = commands.add_parser("auth", help="verify the saved session")
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
    commands.add_parser("status", help="show session and browser state")
    commands.add_parser("logout", help="delete saved credentials")
    return parser


def _provider_main(app, argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    accounts = app.accounts
    accounts.cdp_url = args.cdp_url
    key = args.account or accounts.active()

    try:
        if args.command is None:
            if not sys.stdin.isatty():
                raise ChatGPTError(
                    "interactive mode requires a terminal; use new or send for piped input"
                )
            from tui.application import run_fullscreen

            return run_fullscreen(app, cdp_url=args.cdp_url)
        if args.command == "resume":
            from tui.application import run_fullscreen

            return run_fullscreen(app, conversation_id=args.conversation_id, cdp_url=args.cdp_url)

        if not key:
            raise ValueError("Önce hesap ekleyin")

        if args.command == "login":
            print(
                json.dumps(accounts.session_action(key, "connect_browser"), separators=(",", ":"))
            )
        elif args.command == "logout":
            print(json.dumps(accounts.session_action(key, "logout"), separators=(",", ":")))
        elif args.command == "auth":
            action = "verify" if args.refresh else "status"
            result = accounts.session_action(key, action)
            if not args.refresh and not result.get("authenticated"):
                result = accounts.session_action(key, "verify")
            print(json.dumps(result, separators=(",", ":")))
        elif args.command == "status":
            state = accounts.status(key)
            state["browser_connected"] = accounts.browser_connected(key)
            print(json.dumps(state, separators=(",", ":")))
        elif args.command == "list":
            if args.limit is not None and args.limit < 1:
                raise ValueError("--limit must be greater than zero")
            items = accounts.list_chats(account_keys={key}, force=True).get("items", [])
            if not args.all and args.limit is not None:
                items = items[: args.limit]
            print_conversation_list(items, args.output)
        elif args.command == "show":
            conversation = accounts.load(args.conversation_id, key, force=True)
            print_conversation(conversation, args.output)
        elif args.command == "rename":
            title = " ".join(args.title).strip()
            accounts.callbacks(key)["rename_conversation"](args.conversation_id, title)
            print(
                json.dumps(
                    {"conversation_id": args.conversation_id, "title": title},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        elif args.command in ("send", "new"):
            prompt = " ".join(args.prompt).strip()
            if not prompt and not sys.stdin.isatty():
                prompt = sys.stdin.read().strip()
            if not prompt:
                raise ValueError("provide a prompt argument or pipe one on stdin")
            result = stream_prompt(
                accounts,
                key,
                args.conversation_id if args.command == "send" else None,
                prompt,
                args.output,
            )
            if args.command == "new" and args.output == "text":
                print(f"\n[conversation {result.get('conversation_id')}]", file=sys.stderr)
    except KeyboardInterrupt:
        print("\n[interrupted]", file=sys.stderr)
        return 130
    except (WebAIProviderError, ValueError, RuntimeError) as error:
        print(f"siopechat: {error}", file=sys.stderr)
        return 1
    return 0


def _workspace(app, argv: list[str]) -> int:
    from tui.workspaces import Workspaces

    parser = argparse.ArgumentParser(prog="siopechat workspace")
    parser.add_argument("--db")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("create").add_argument("name")
    commands.add_parser("list")
    commands.add_parser("show").add_argument("id")
    commands.add_parser("delete").add_argument("id")
    args = parser.parse_args(argv)
    service = Workspaces(app.get("data_root") / "workspaces.sqlite3")
    kwargs = {"path": args.db}
    if args.command == "create":
        kwargs["name"] = args.name
    if args.command in ("show", "delete"):
        kwargs["workspace_id"] = args.id
    try:
        result = service.execute_sync(args.command, **kwargs)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (LookupError, ValueError, OSError, sqlite3.Error) as error:
        parser.exit(1, f"workspace: {error}\n")


async def _management(app, argv: list[str]) -> int:
    try:
        if argv[0] == "workspace":
            return _workspace(app, argv[1:])
        if argv[0] == "settings":
            if len(argv) > 1:
                if len(argv) != 4 or argv[1] != "set":
                    raise ValueError("usage: siopechat settings [set KEY JSON_VALUE]")
                key, raw = argv[2:]
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError:
                    value = raw
                if key.startswith("keybindings."):
                    section, field = "keybindings", key.removeprefix("keybindings.")
                elif key.startswith("providers."):
                    section, field = key.rsplit(".", 1)
                else:
                    section, field = "application", key
                await app.actions.invoke("settings.change", section=section, key=field, value=value)
            print(json.dumps(app.settings.snapshot(), ensure_ascii=False, indent=2))
            return 0
        if argv[0] == "actions":
            print(
                json.dumps(
                    [
                        {"id": action.id, "title": action.title}
                        for action in app.actions.search(" ".join(argv[1:]))
                    ]
                )
            )
            return 0
        if argv[0] == "action":
            if len(argv) not in (2, 3):
                raise ValueError("usage: siopechat action ID [JSON_ARGUMENTS]")
            arguments = json.loads(argv[2]) if len(argv) == 3 else {}
            if not isinstance(arguments, dict):
                raise ValueError("action arguments must be a JSON object")
            print(json.dumps(await app.actions.invoke(argv[1], **arguments), ensure_ascii=False))
            return 0
        if argv[0] == "providers":
            config = app.settings.snapshot()["providers"]
            descriptions = [
                {
                    "id": ptype.value,
                    "title": PROVIDERS[ptype.value][0],
                    "origin": PROVIDERS[ptype.value][1],
                    "enabled": config.get(ptype.value, {}).get("enabled", True),
                    "capabilities": sorted(cap.value for cap in provider_capabilities(ptype.value)),
                }
                for ptype in WebAIProviderType
            ]
            print(json.dumps(descriptions, ensure_ascii=False, indent=2))
            return 0
    except (ImportError, OSError, ValueError, KeyError, LookupError, sqlite3.Error) as error:
        print(f"siopechat: {error}", file=sys.stderr)
        return 1
    return 0


def main(app, argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    management = {"workspace", "settings", "actions", "action", "providers"}
    if args and args[0] in management:

        async def execute() -> int:
            try:
                return await _management(app, args)
            finally:
                await app.runtime.aclose()

        return asyncio.run(execute())
    try:
        return _provider_main(app, args)
    finally:
        app.runtime.close()

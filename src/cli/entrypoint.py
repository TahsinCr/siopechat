"""Command-line presentation and dispatch through the shared runtime."""

import asyncio
import json
import sys
from dataclasses import asdict


async def _management(api, argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if args and args[0] == "workspace":
            from cli.workspaces import main as workspace_main

            return await workspace_main(api, args[1:])
        if args and args[0] == "settings":
            if len(args) > 1:
                if len(args) != 4 or args[1] != "set":
                    raise ValueError("usage: siopechat settings [set KEY JSON_VALUE]")
                key, raw = args[2:]
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError:
                    value = raw
                if key.startswith("keybindings."):
                    section, field = ("keybindings", key.removeprefix("keybindings."))
                elif key.startswith("extensions."):
                    owner, field = key.removeprefix("extensions.").rsplit(".", 1)
                    section = f"extensions.{owner}"
                else:
                    section, field = ("application", key)
                await api.actions.invoke("settings.change", section=section, key=field, value=value)
            print(json.dumps(api.settings.snapshot(), ensure_ascii=False, indent=2))
            return 0
        if args and args[0] == "extensions":
            manager = api.get("extensions")
            if len(args) > 1:
                if len(args) != 3 or args[1] not in ("enable", "disable"):
                    raise ValueError("usage: siopechat extensions [enable|disable ID]")
                await api.actions.invoke(f"extensions.{args[1]}", extension_id=args[2])
            print(json.dumps({key: asdict(value) for key, value in manager.statuses().items()}))
            return 0
        if args and args[0] == "actions":
            print(
                json.dumps(
                    [
                        {"id": action.id, "title": action.title}
                        for action in api.actions.search(" ".join(args[1:]))
                    ]
                )
            )
            return 0
        if args and args[0] == "action":
            if len(args) not in (2, 3):
                raise ValueError("usage: siopechat action ID [JSON_ARGUMENTS]")
            arguments = json.loads(args[2]) if len(args) == 3 else {}
            if not isinstance(arguments, dict):
                raise ValueError("action arguments must be a JSON object")
            print(json.dumps(await api.actions.invoke(args[1], **arguments), ensure_ascii=False))
            return 0
        if args and args[0] == "providers":

            async def descriptions():
                return [asdict(await api.providers.describe(key)) for key in api.providers.ids()]

            print(json.dumps(await descriptions(), ensure_ascii=False, indent=2))
            return 0
        from cli.commands import main as cli_main

        return cli_main(api, args)
    except (ImportError, OSError, ValueError, KeyError, LookupError) as error:
        print(f"siopechat: {error}", file=sys.stderr)
        return 1


def main(api, argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    management = {"workspace", "settings", "extensions", "actions", "action", "providers"}
    if args and args[0] in management:

        async def execute():
            try:
                return await _management(api, args)
            finally:
                await api.runtime.aclose()

        return asyncio.run(execute())
    from .commands import main as chat_main

    try:
        return chat_main(api, args)
    finally:
        api.runtime.close()

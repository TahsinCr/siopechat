"""Workspace presentation; operations are contributed by the layout extension."""

import argparse
import json
import sqlite3


async def main(api, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("create").add_argument("name")
    commands.add_parser("list")
    commands.add_parser("show").add_argument("id")
    commands.add_parser("delete").add_argument("id")
    args = parser.parse_args(argv)
    values = {"path": args.db}
    if args.command == "create":
        values["name"] = args.name
    if args.command in ("show", "delete"):
        values["workspace_id"] = args.id
    try:
        result = await api.actions.invoke(f"layout.{args.command}", **values)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (LookupError, ValueError, OSError, sqlite3.Error) as error:
        parser.exit(1, f"workspace: {error}; check whether layout extension is enabled\n")

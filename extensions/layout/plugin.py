"""Bundled workspace service, supplied through the shared extension API."""

import json
import uuid
from dataclasses import asdict
from pathlib import Path

from extension import Action, ExtensionContext, ExtensionSpec, SettingField

from .models import Workspace
from .repository import WorkspaceStore


class Workspaces:
    def __init__(self, path: Path):
        self.path = path

    @staticmethod
    def create_layout(view):
        from .panes import SplitLayout

        return SplitLayout(view)

    async def execute(self, command: str, name=None, workspace_id=None, path=None):
        store = await WorkspaceStore.open(Path(path) if path else self.path)
        if command == "create":
            value = Workspace(uuid.uuid4().hex, name)
            await store.save(value)
            return asdict(value)
        if command == "show":
            return asdict(await store.load(workspace_id))
        if command == "delete":
            return {"deleted": await store.delete(workspace_id)}
        return [{"id": key, "name": title} for key, title in await store.list()]


def activate(context: ExtensionContext) -> None:
    context.settings(SettingField("enabled", bool, True, "layout.enabled"))
    for locale in ("en", "tr"):
        messages = json.loads(
            (Path(__file__).parent / "languages" / f"{locale}.json").read_text(encoding="utf-8")
        )
        context.translations(locale, messages)
    root = context.api.get("paths")
    service = Workspaces(root / "data" / "workspaces.sqlite3")
    context.provide("workspaces", service)
    for command in ("list", "create", "show", "delete"):

        def invoke(command=command, **kwargs):
            return service.execute(command, **kwargs)

        context.action(Action(f"layout.{command}", f"Workspace: {command}", invoke))


SPEC = ExtensionSpec("layout", activate)

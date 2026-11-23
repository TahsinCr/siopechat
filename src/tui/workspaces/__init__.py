"""Built-in saved workspaces and terminal split layouts."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from pathlib import Path

from .models import Tab, TabGroup, Workspace
from .panes import SplitLayout
from .repository import WorkspaceStore

__all__ = [
    "SplitLayout",
    "Tab",
    "TabGroup",
    "Workspace",
    "WorkspaceStore",
    "Workspaces",
]


class Workspaces:
    """Manage saved workspace layouts for the terminal UI and CLI."""

    def __init__(self, path: Path):
        self.path = Path(path)

    @staticmethod
    def create_layout(view) -> SplitLayout:
        return SplitLayout(view)

    def execute_sync(self, command: str, name=None, workspace_id=None, path=None, workspace=None):
        store = WorkspaceStore.open_sync(Path(path) if path else self.path)
        if command == "create":
            value = (
                Workspace.from_dict(workspace)
                if workspace is not None
                else Workspace(uuid.uuid4().hex, name)
            )
            store.save_sync(value)
            return json.loads(json.dumps(asdict(value)))
        if command == "show":
            return json.loads(json.dumps(asdict(store.load_sync(workspace_id))))
        if command == "delete":
            return {"deleted": store.delete_sync(workspace_id)}
        return [{"id": key, "name": title} for key, title in store.list_sync()]

    async def execute(self, command: str, name=None, workspace_id=None, path=None, workspace=None):
        store = await WorkspaceStore.open(Path(path) if path else self.path)
        if command == "create":
            value = (
                Workspace.from_dict(workspace)
                if workspace is not None
                else Workspace(uuid.uuid4().hex, name)
            )
            await store.save(value)
            return json.loads(json.dumps(asdict(value)))
        if command == "show":
            return json.loads(json.dumps(asdict(await store.load(workspace_id))))
        if command == "delete":
            return {"deleted": await store.delete(workspace_id)}
        return [{"id": key, "name": title} for key, title in await store.list()]

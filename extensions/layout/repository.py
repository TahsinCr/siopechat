"""SQLite implementation of workspace persistence, outside the domain core."""

import asyncio
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from utils.process import set_private_permissions

from .models import Workspace


class WorkspaceStore:
    """Short SQLite transactions run in worker threads, never in the UI loop.

    Owns its database file; do not pass an unrelated application's database.
    No connection stays open and no writes occur per streamed token.
    """

    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve()

    @classmethod
    async def open(cls, path: Path) -> "WorkspaceStore":
        store = cls(path)
        await asyncio.to_thread(store._initialize)
        return store

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        set_private_permissions(self.path, 0o600)
        with closing(sqlite3.connect(self.path, timeout=3)) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1):
                    raise ValueError("unsupported core database schema version")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS workspaces "
                    "(id TEXT PRIMARY KEY, name TEXT NOT NULL, body TEXT NOT NULL)"
                )
                connection.execute("PRAGMA user_version = 1")

    def _execute(self, sql: str, parameters: tuple = ()) -> tuple[list[tuple], int]:
        with closing(sqlite3.connect(self.path, timeout=3)) as connection:
            with connection:
                cursor = connection.execute(sql, parameters)
                return cursor.fetchall(), cursor.rowcount

    async def save(self, workspace: Workspace) -> None:
        workspace.validate()
        body = json.dumps(asdict(workspace), ensure_ascii=False, separators=(",", ":"))
        await asyncio.to_thread(
            self._execute,
            "INSERT INTO workspaces(id,name,body) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name,body=excluded.body",
            (workspace.id, workspace.name, body),
        )

    async def load(self, workspace_id: str) -> Workspace:
        rows, _ = await asyncio.to_thread(
            self._execute, "SELECT body FROM workspaces WHERE id=?", (workspace_id,)
        )
        if not rows:
            raise KeyError(workspace_id)
        return Workspace.from_dict(json.loads(rows[0][0]))

    async def list(self) -> tuple[tuple[str, str], ...]:
        rows, _ = await asyncio.to_thread(
            self._execute, "SELECT id,name FROM workspaces ORDER BY name,id"
        )
        return tuple(rows)

    async def delete(self, workspace_id: str) -> bool:
        _, count = await asyncio.to_thread(
            self._execute, "DELETE FROM workspaces WHERE id=?", (workspace_id,)
        )
        return count > 0

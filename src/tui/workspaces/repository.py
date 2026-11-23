"""SQLite implementation of workspace persistence, outside the domain core."""

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from utils.sqlite import SQLiteRepositoryBase

from .models import Workspace


class WorkspaceStore(SQLiteRepositoryBase):
    """Short SQLite transactions run in worker threads, never in the UI loop.

    Owns its database file; do not pass an unrelated application's database.
    No connection stays open and no writes occur per streamed token.
    """

    def __init__(self, path: Path):
        super().__init__(Path(path).expanduser().resolve(), timeout=3)

    @classmethod
    def open_sync(cls, path: Path) -> "WorkspaceStore":
        store = cls(path)
        store._initialize()
        return store

    @classmethod
    async def open(cls, path: Path) -> "WorkspaceStore":
        store = cls(path)
        await asyncio.to_thread(store._initialize)
        return store

    def _initialize(self) -> None:
        self.prepare_private_database()
        with self.connect() as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] == 1:
                return
        with self.transaction(immediate=True) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("unsupported core database schema version")
            if version == 1:
                return
            connection.execute(
                "CREATE TABLE IF NOT EXISTS workspaces "
                "(id TEXT PRIMARY KEY, name TEXT NOT NULL, body TEXT NOT NULL)"
            )
            connection.execute("PRAGMA user_version = 1")

    def _execute(self, sql: str, parameters: tuple = ()) -> tuple[list[tuple], int]:
        with self.connect() as connection:
            cursor = connection.execute(sql, parameters)
            return cursor.fetchall(), cursor.rowcount

    def save_sync(self, workspace: Workspace) -> None:
        workspace.validate()
        body = json.dumps(asdict(workspace), ensure_ascii=False, separators=(",", ":"))
        self._execute(
            "INSERT INTO workspaces(id,name,body) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name,body=excluded.body",
            (workspace.id, workspace.name, body),
        )

    def load_sync(self, workspace_id: str) -> Workspace:
        rows, _ = self._execute("SELECT body FROM workspaces WHERE id=?", (workspace_id,))
        if not rows:
            raise KeyError(workspace_id)
        return Workspace.from_dict(json.loads(rows[0][0]))

    def list_sync(self) -> tuple[tuple[str, str], ...]:
        rows, _ = self._execute("SELECT id,name FROM workspaces ORDER BY name,id")
        return tuple(rows)

    def delete_sync(self, workspace_id: str) -> bool:
        _, count = self._execute("DELETE FROM workspaces WHERE id=?", (workspace_id,))
        return count > 0

    async def save(self, workspace: Workspace) -> None:
        await asyncio.to_thread(self.save_sync, workspace)

    async def load(self, workspace_id: str) -> Workspace:
        return await asyncio.to_thread(self.load_sync, workspace_id)

    async def list(self) -> tuple[tuple[str, str], ...]:
        return await asyncio.to_thread(self.list_sync)

    async def delete(self, workspace_id: str) -> bool:
        return await asyncio.to_thread(self.delete_sync, workspace_id)

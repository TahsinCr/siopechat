import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path

from extensions.layout.models import Tab, TabGroup, Workspace
from extensions.layout.repository import WorkspaceStore


def workspace():
    return Workspace(
        "research",
        "Araştırma",
        (
            TabGroup(
                "compare",
                (
                    Tab(
                        "gpt-tab",
                        "GPT",
                        provider_id="gpt",
                        account_id="primary",
                        chat_id="gpt-chat",
                        conversation_id="remote-gpt",
                    ),
                    Tab(
                        "gemini-tab",
                        "Gemini",
                        provider_id="gemini",
                        account_id="primary",
                        chat_id="gemini-chat",
                    ),
                ),
                "gemini-tab",
                "columns",
            ),
            TabGroup("tools", (Tab("projects", "Projeler", kind="projects"),), "projects"),
        ),
        "compare",
    )


class WorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "data/core.sqlite3"
        self.store = await WorkspaceStore.open(self.path)

    async def test_round_trip_reopen_preserves_order_and_mixed_providers(self):
        saved = workspace()
        await self.store.save(saved)
        reopened = await WorkspaceStore.open(self.path)
        self.assertEqual(await reopened.load(saved.id), saved)
        self.assertEqual(await reopened.list(), (("research", "Araştırma"),))

    async def test_update_delete_and_missing_workspace(self):
        saved = workspace()
        await self.store.save(saved)
        updated = replace(saved, name="İkinci çalışma", active_group_id="tools")
        await self.store.save(updated)
        self.assertEqual(await self.store.load(saved.id), updated)
        self.assertTrue(await self.store.delete(saved.id))
        self.assertFalse(await self.store.delete(saved.id))
        with self.assertRaises(KeyError):
            await self.store.load(saved.id)

    async def test_invalid_layout_does_not_overwrite_saved_workspace(self):
        saved = workspace()
        await self.store.save(saved)
        invalid = replace(saved, active_group_id="missing")
        with self.assertRaises(ValueError):
            await self.store.save(invalid)
        self.assertEqual(await self.store.load(saved.id), saved)

    async def test_duplicate_tabs_invalid_selection_and_incomplete_chat_rejected(self):
        saved = workspace()
        invalid_groups = [
            (saved.groups[0], saved.groups[0]),
            (replace(saved.groups[0], active_tab_id="projects"),),
            (replace(saved.groups[0], layout="invalid"),),
            (TabGroup("one", (Tab("incomplete", "Chat"),)),),
            (
                TabGroup("one", (saved.groups[0].tabs[0],)),
                TabGroup("two", (saved.groups[0].tabs[0],)),
            ),
        ]
        for groups in invalid_groups:
            with self.subTest(groups=groups), self.assertRaises(ValueError):
                await self.store.save(replace(saved, groups=groups, active_group_id=None))

    async def test_parallel_saves_are_atomic_and_quoted_ids_are_safe(self):
        workspaces = [replace(workspace(), id=f"id'{index}") for index in range(8)]
        await asyncio.gather(*(self.store.save(value) for value in workspaces))
        self.assertEqual(len(await self.store.list()), 8)
        for saved in workspaces:
            self.assertEqual(await self.store.load(saved.id), saved)

    async def test_schema_version_rejects_newer_database(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA user_version = 99")
        with self.assertRaises(ValueError):
            await WorkspaceStore.open(self.path)

    async def test_malformed_documents_have_controlled_validation_errors(self):
        values = [
            None,
            [],
            {"id": "x", "name": None},
            {"id": "x", "name": "x", "groups": "invalid"},
            {"id": "x", "name": "x", "groups": [None]},
        ]
        valid = json.loads(json.dumps(asdict(workspace())))
        valid["groups"][0]["tabs"][0]["account_id"] = 123
        values.append(valid)
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                Workspace.from_dict(value)

    async def test_corrupt_stored_document_is_not_returned_as_a_workspace(self):
        await self.store.save(workspace())
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE workspaces SET body='null'")
        with self.assertRaises(ValueError):
            await self.store.load("research")

    @unittest.skipUnless(os.name == "posix", "POSIX file permissions")
    async def test_database_has_private_permissions(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

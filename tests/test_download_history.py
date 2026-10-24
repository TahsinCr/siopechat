"""Persistent ledger and actual terminal navigation regressions."""

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from bootstrap import create_application
from downloads import Downloads
from extensions.archive.repository import Files, SQLiteRepository
from extensions.archive.sync import Archive
from providers.files import DownloadSource
from settings import Settings
from tests.cli_support import immediate, terminal


class DownloadHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = SQLiteRepository(self.root / "archive.db")
        self.archive = Archive(self.repo, Files(self.root / "files"))
        self.addCleanup(self.archive.close)
        self.accounts = SimpleNamespace(
            archive=lambda: self.archive,
            resolve_download=lambda *args: DownloadSource(
                "https://example.com/a.txt", force_download=True
            ),
        )
        self.service = self.make_service()

    def make_service(self):
        return Downloads(
            Settings(self.root / "config.json"),
            self.root,
            self.accounts,
            lambda **kw: httpx.Client(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(
                        200, content=b"content", headers={"content-type": "text/plain"}
                    )
                ),
                **kw,
            ),
            resolver=lambda host, port, **kw: [(2, 1, 6, "", ("93.184.216.34", port))],
        )

    def download(self):
        return self.service.fetch(
            "gemini/a",
            "https://example.com/a.txt?secret=not-stored",
            "@gemini/a/chat",
            title="Source chat",
        )

    def test_restart_deleted_file_and_source_chat_keep_history(self):
        result = self.download()
        row = self.service.snapshot()[0]
        self.assertEqual(row["state"], "completed")
        self.assertNotIn("secret", json.dumps(self.archive.downloads.list()))
        Path(result["path"]).unlink()
        self.archive.delete("gemini/a", "conversation", "chat")
        fresh = self.make_service()
        rows = fresh.refresh()
        self.assertEqual(rows[0]["state"], "deleted")
        self.assertTrue(rows[0]["chat_deleted"])
        self.assertEqual(rows[0]["title"], "Source chat")
        fresh.remove(row["id"])
        self.assertEqual(self.make_service().refresh(), [])

    def test_opening_existing_history_does_not_rewrite_unchanged_rows(self):
        self.download()
        fresh = self.make_service()
        with patch.object(
            self.archive.downloads,
            "put_many",
            wraps=self.archive.downloads.put_many,
        ) as put_many:
            fresh.refresh()
        put_many.assert_not_called()

    def test_bulk_history_write_rejects_stale_revision(self):
        history = self.archive.downloads
        initial = {
            "id": "revisioned",
            "created": 1,
            "updated": 1,
            "state": "completed",
            "account": "gemini/a",
            "name": "initial.txt",
            "path": None,
        }
        self.assertEqual(history.put_many([{**initial, "_revision": 1}]), {})
        newer = {**initial, "name": "latest.txt", "_revision": 2}
        self.assertEqual(history.put_many([newer]), {})
        stale = {**initial, "name": "stale.txt", "_revision": 2}

        rejected = history.put_many([stale])

        self.assertEqual(rejected["revisioned"]["name"], "latest.txt")
        stored = next(row for row in history.list() if row["id"] == "revisioned")
        self.assertEqual(stored["name"], "latest.txt")
        self.assertEqual(stored["_revision"], 2)

    def test_refresh_reads_each_download_directory_once(self):
        first = self.download()
        self.service.fetch("gemini/a", "https://example.com/b.txt")
        with patch("downloads.os.scandir", wraps=os.scandir) as scandir:
            rows = self.service.refresh()

        self.assertEqual(len(rows), 2)
        self.assertTrue(all(Path(row["path"]).parent == Path(first["path"]).parent for row in rows))
        scandir.assert_called_once()

    def test_delete_file_retains_row_until_explicit_forget(self):
        result = self.download()
        identity = self.service.snapshot()[0]["id"]
        self.service.remove(identity, delete_file=True)
        self.assertFalse(Path(result["path"]).exists())
        self.assertEqual(self.service.refresh()[0]["state"], "deleted")
        self.assertEqual(self.archive.downloads.list()[0]["state"], "deleted")
        self.service.remove(identity)
        self.assertEqual(self.archive.downloads.list(), [])

    def test_progress_is_live_and_active_record_cannot_be_removed(self):
        observations = []

        def progress(name, size, total):
            rows = self.service.active_snapshot()
            observations.append((rows[0]["state"], rows[0]["size"], total))
            with self.assertRaises(ValueError):
                self.service.remove(rows[0]["id"])

        self.service.fetch("gemini/a", "https://example.com/a.txt", progress=progress)
        self.assertTrue(any(size > 0 for _, size, _ in observations))
        self.assertEqual(self.service.active_snapshot(), [])
        self.assertEqual(self.service.snapshot()[0]["size"], 7)

    def test_interrupted_record_and_schema_one_migrate_without_data_loss(self):
        self.archive.put("gemini/a", "conversation", "chat", {"title": "Saved", "messages": []})
        self.archive.downloads.put_many(
            [
                dict(
                    id="old",
                    created=1,
                    state="downloading",
                    account="gemini/a",
                    conversation_id="chat",
                    size=3,
                    name="old.txt",
                    path=None,
                )
            ]
        )
        with closing(sqlite3.connect(self.repo.path)) as db:
            db.execute("UPDATE schema_version SET version=1")
            db.commit()
        reopened = SQLiteRepository(self.repo.path)
        self.assertEqual(reopened.get("gemini/a", "conversation", "chat")["title"], "Saved")
        self.assertEqual(self.make_service().refresh()[0]["state"], "interrupted")
        with closing(sqlite3.connect(self.repo.path)) as db:
            self.assertEqual(db.execute("SELECT version FROM schema_version").fetchall(), [(5,)])
            self.assertIn(
                "revision", {row[1] for row in db.execute("PRAGMA table_info(downloads)")}
            )

    def test_archive_failure_does_not_fail_saved_download(self):
        self.accounts.archive = Mock(side_effect=OSError("unavailable"))
        self.assertEqual(self.download()["kind"], "file")
        self.assertEqual(self.service.snapshot()[0]["state"], "completed")
        self.assertTrue(self.service.snapshot()[0]["history_warning"])


class DownloadWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_rows_format_only_terminal_requested_lines(self):
        from cli.downloads import _DownloadsRowsControl

        class Panel:
            rows = [None] * 20_000
            index = 0
            render_revision = 1

            def __init__(self):
                self.rendered = []

            def render_row(self, index, width):
                self.rendered.append(index)
                return [("", str(index))]

        panel = Panel()
        control = _DownloadsRowsControl(panel)
        content = control.create_content(100, 24)

        self.assertEqual(content.line_count, 20_000)
        self.assertEqual(content.get_line(0), [("", "0")])
        self.assertEqual(content.get_line(1), [("", "1")])
        self.assertEqual(panel.rendered, [0, 1])

    async def test_download_selection_invalidates_old_and_new_cached_rows(self):
        with terminal() as (view, _):
            panel = view.downloads_panel
            panel.rows = [("file", {}), ("file", {})]
            panel.index = 0
            panel.render_revision += 1
            panel.render_row = lambda index, _width: [
                ("class:message.selected" if panel.index == index else "", str(index))
            ]

            content = panel.control.create_content(40, 2)
            self.assertEqual(content.get_line(0)[0][0], "class:message.selected")
            self.assertEqual(content.get_line(1)[0][0], "")

            panel.move(1)

            content = panel.control.create_content(40, 2)
            self.assertEqual(content.get_line(0)[0][0], "")
            self.assertEqual(content.get_line(1)[0][0], "class:message.selected")

    async def test_filtered_active_download_updates_its_own_record(self):
        with terminal() as (view, _):
            panel = view.downloads_panel
            panel.records = [
                dict(id="done", state="completed", created=1, account="g/a", name="done"),
                dict(id="live", state="downloading", created=2, account="g/a", name="live"),
            ]
            panel.filter_index = 1
            panel.rebuild()
            self.assertEqual(panel._records_by_id, {"done": 0, "live": 1})

            updated = {**panel.records[1], "size": 12}
            panel._update_active_rows([updated])

            self.assertEqual(panel.records[1]["id"], "live")
            self.assertEqual(panel.records[1]["size"], 12)
            self.assertTrue(all(row[1].get("size") == 12 for row in panel.rows if row[0] != "day"))

    async def test_download_tab_reuses_switches_and_keyboard_closes(self):
        with tempfile.TemporaryDirectory() as folder:
            api = create_application(Path(folder))
            with terminal() as (view, pipe):
                # Exercise the actual provider-free terminal, without authentication work.
                service = api.get("downloads")
                panel = view.downloads_panel
                panel.service = lambda: service
                view._read_in_background = immediate
                run = asyncio.create_task(view.app.run_async())
                try:
                    await asyncio.sleep(0.05)
                    pipe.send_bytes(b"\x1bj")
                    await asyncio.sleep(0.08)
                    self.assertEqual(view.tab_kind, "downloads")
                    self.assertIs(view.app.layout.current_control, panel.search.control)
                    view.switch_tab(-1)
                    self.assertEqual(view.tab_kind, "chat")
                    panel.open()
                    self.assertEqual(len(view.tabs), 2)
                    pipe.send_bytes(b"\x17")
                    await asyncio.sleep(0.08)
                    self.assertEqual(view.tab_kind, "chat")
                    self.assertFalse(any(t.get("kind") == "downloads" for t in view.tabs))
                finally:
                    view.app.exit()
                    await run
                    await api.runtime.aclose()

    async def test_date_collapse_filter_sort_and_deleted_source(self):
        with terminal() as (view, _):
            panel = view.downloads_panel
            panel.records = [
                dict(
                    id=str(i),
                    created=10 + i,
                    account="gemini/a",
                    name=name,
                    state=state,
                    size=i,
                    total=i,
                    title="Source",
                    conversation_id="chat",
                    chat_deleted=True,
                )
                for i, (name, state) in enumerate((("z.txt", "completed"), ("a.txt", "deleted")))
            ]
            panel.rebuild()
            self.assertEqual(len(panel.rows), 5)
            panel.activate()
            self.assertEqual(len(panel.rows), 1)
            panel.activate()
            panel.sort_index = 2
            panel.rebuild()
            self.assertEqual(panel.rows[1][1]["name"], "a.txt")
            panel.filter_index = 2
            panel.rebuild()
            self.assertEqual(len(panel.rows), 3)
            panel.index = 2
            panel.open_chat()
            self.assertIn("silindi", panel.message.lower())

    async def test_responsive_page_and_keyboard_date_groups(self):
        from tests.cli_support import ResizableOutput

        output = ResizableOutput()
        with terminal(output=output) as (view, pipe):
            panel = view.downloads_panel
            view.new_tab()
            view.tab_kind, view.title = "downloads", "İndirmeler"
            view._save_active_tab()
            panel.records = [
                dict(
                    id="one",
                    created=1,
                    account="gemini/a",
                    name="photo.png",
                    state="completed",
                    size=100,
                    title="Source",
                    conversation_id="chat",
                )
            ]
            panel.rebuild()
            view.app.layout.focus(panel.control)
            run = asyncio.create_task(view.app.run_async())
            try:
                await asyncio.sleep(0.05)
                pipe.send_bytes(b"\r")
                await asyncio.sleep(0.05)
                self.assertEqual(len(panel.rows), 1)
                pipe.send_bytes(b"\r")
                await asyncio.sleep(0.05)
                self.assertEqual(len(panel.rows), 3)
                panel.move(1)
                for columns, rows in ((120, 34), (80, 24), (60, 18), (40, 12)):
                    output.columns, output.rows = columns, rows
                    view.app.invalidate()
                    await asyncio.sleep(0.06)
                    screen = view.app.renderer._last_screen
                    text = "\n".join(
                        "".join(screen.data_buffer[y][x].char for x in range(columns))
                        for y in range(rows)
                    )
                    self.assertNotIn("Window too small", text)
                    self.assertIn("Kapat", text)
                    self.assertIn("photo", text)
            finally:
                view.app.exit()
                await run

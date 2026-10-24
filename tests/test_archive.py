"""Archive reads, writes, schema and maintenance behavior."""

import hashlib
import io
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from extensions.archive import repository as archive_repository
from extensions.archive.repository import Files, SQLiteRepository
from extensions.archive.sync import Archive


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.repository = SQLiteRepository(self.root / "archive.sqlite3")
        self.archive = Archive(self.repository, Files(self.root / "files"), memory_bytes=2048)
        self.addCleanup(self.archive.close)

    def wait(self):
        until = time.monotonic() + 10
        while any(x["state"] == "syncing" for x in self.archive.states()):
            self.assertLess(time.monotonic(), until)
            time.sleep(0.005)

    def test_account_isolation_roundtrip_reasoning_and_resources(self):
        path, digest = self.archive.files.save(b"file contents")
        value = {
            "id": "one",
            "title": "A",
            "project_id": "@gpt/a/project",
            "messages": [
                {
                    "id": "1",
                    "role": "assistant",
                    "channel": "analysis",
                    "text": "Visible summary",
                    "metadata": {"source": "provider"},
                },
                {
                    "id": "2",
                    "role": "assistant",
                    "text": "Answer",
                    "content": [
                        {
                            "kind": "image",
                            "resource": {
                                "id": "img",
                                "name": "image",
                                "path": path,
                                "sha256": digest,
                            },
                        }
                    ],
                },
            ],
        }
        self.archive.put("chatgpt/a", "conversation", "one", value)
        self.assertEqual(self.archive.get("chatgpt/a", "conversation", "one"), value)
        self.assertIsNone(self.archive.get("gemini/a", "conversation", "one"))
        self.assertEqual((self.root / "files" / path).read_bytes(), b"file contents")
        with closing(sqlite3.connect(self.repository.path)) as db:
            self.assertEqual(
                db.execute("SELECT channel FROM messages ORDER BY ordinal").fetchone()[0],
                "analysis",
            )
            self.assertEqual(db.execute("SELECT path FROM resources").fetchone()[0], path)
            self.assertEqual(db.execute("SELECT count(*) FROM memberships").fetchone()[0], 1)
        self.assertFalse(self.repository.put("chatgpt/a", "conversation", "one", value))
        fresh = SQLiteRepository(self.repository.path)
        self.assertEqual(fresh.get("chatgpt/a", "conversation", "one"), value)

    def test_conversation_digest_does_not_materialize_a_second_full_json_record(self):
        value = {
            "title": "Long history",
            "messages": [
                {"id": str(index), "role": "assistant", "text": "answer" * 200}
                for index in range(20)
            ],
        }
        expected = hashlib.sha256(archive_repository.encode(value).encode()).hexdigest()
        original_encode = archive_repository.encode

        with patch("extensions.archive.repository.encode", wraps=original_encode) as encode_value:
            self.repository.put("chatgpt/a", "conversation", "long", value)

        self.assertFalse(any(call.args[0] is value for call in encode_value.call_args_list))
        with closing(sqlite3.connect(self.repository.path)) as db:
            digest = db.execute(
                "SELECT digest FROM records WHERE account=? AND kind=? AND remote=?",
                ("chatgpt/a", "conversation", "long"),
            ).fetchone()[0]
        self.assertEqual(digest, expected)
        self.assertFalse(self.repository.put("chatgpt/a", "conversation", "long", value))

    def test_cached_read_returns_before_remote_and_single_flight(self):
        self.archive.put("gemini/a", "conversation", "one", {"messages": [], "title": "old"})
        entered, release = threading.Event(), threading.Event()

        def fetch():
            entered.set()
            self.assertTrue(release.wait(3))
            return {"messages": [], "title": "new"}

        start = time.monotonic()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda _: self.archive.read("gemini/a", "conversation", "one", fetch), range(16)
                )
            )
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertTrue(
            entered.wait(5),
            f"archive closed={self.archive.closed}, deleted={self.archive._deleted}, "
            f"pending={tuple(self.archive._pending)}, states={self.archive.states()}",
        )
        self.assertEqual({v["title"] for v in results}, {"old"})
        self.assertEqual(len(self.archive._pending), 1)
        release.set()
        self.wait()
        self.assertEqual(self.archive.get("gemini/a", "conversation", "one")["title"], "new")

    def test_cold_record_syncs_when_monotonic_uptime_is_under_one_minute(self):
        from unittest.mock import patch

        with patch("extensions.archive.sync.time.monotonic", return_value=15):
            value = self.archive.read(
                "gemini/a", "conversation", "cold", lambda: {"title": "remote"}
            )
        self.assertEqual(value, {"title": "remote"})
        self.assertEqual(self.archive.get("gemini/a", "conversation", "cold"), value)

    def test_late_read_cannot_overwrite_completed_send(self):
        self.archive.put("chatgpt/a", "conversation", "one", {"title": "old"})
        release = threading.Event()
        self.archive.read(
            "chatgpt/a", "conversation", "one", lambda: release.wait(3) and {"title": "stale"}
        )
        self.archive.put("chatgpt/a", "conversation", "one", {"title": "sent"})
        release.set()
        self.wait()
        self.assertEqual(self.archive.get("chatgpt/a", "conversation", "one")["title"], "sent")

    def test_errors_preserve_saved_value_and_explicit_refresh_retries(self):
        self.archive.put("gemini/a", "container", "n", {"name": "Notebook"})
        calls = Mock(side_effect=ValueError("offline"))
        value = self.archive.read("gemini/a", "container", "n", calls)
        self.assertEqual(value["name"], "Notebook")
        self.wait()
        self.assertEqual(self.archive.states()[-1]["state"], "error")
        calls.side_effect = None
        calls.return_value = {"name": "Updated"}
        self.archive.refresh()
        self.wait()
        self.assertEqual(self.archive.get("gemini/a", "container", "n")["name"], "Updated")
        with closing(sqlite3.connect(self.repository.path)) as db:
            self.assertEqual(db.execute("SELECT kind FROM containers").fetchone()[0], "notebook")

    def test_refresh_of_a_failed_cold_record_never_waits_in_the_caller(self):
        release = threading.Event()
        fetch = Mock(side_effect=ValueError("offline"))
        with self.assertRaises(ValueError):
            self.archive.read("gemini/a", "conversation", "missing", fetch)
        self.wait()
        fetch.side_effect = lambda: release.wait(2) and {"title": "Recovered"}
        started = time.monotonic()
        try:
            self.archive.refresh()
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            release.set()
            self.wait()

    def test_hot_memory_is_bounded_and_temporary_data_is_not_archived(self):
        for i in range(25):
            self.archive.put("gemini/a", "conversation", str(i), {"text": "x" * 500})
        self.assertLessEqual(self.archive._size, 2048)
        self.assertLess(len(self.archive._hot), 25)
        self.assertEqual(self.archive.get("gemini/a", "conversation", "0")["text"], "x" * 500)
        self.archive.put(
            "gemini/a", "conversation", "private", {"temporary": True, "text": "secret"}
        )
        self.assertIsNone(self.repository.get("gemini/a", "conversation", "private"))


class ArchiveMaintenanceTests(unittest.TestCase):
    def test_future_schema_is_rejected_without_modification(self):

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "future.db"
            from contextlib import closing

            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE schema_version(version INTEGER PRIMARY KEY)")
                db.execute("INSERT INTO schema_version VALUES(99)")
                db.commit()
            with self.assertRaises(OSError):
                SQLiteRepository(path)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(
                    db.execute("SELECT version FROM schema_version").fetchall(), [(99,)]
                )

    def test_shared_sql_references_survive_until_the_last_owner_is_deleted(self):
        import os

        with tempfile.TemporaryDirectory() as folder:
            repository = SQLiteRepository(Path(folder) / "archive.db")
            files = Files(Path(folder) / "files")
            relative, _ = files.save(b"shared content")
            os.utime(files.root / relative, (time.time() - 7200,) * 2)
            for remote in ("first", "second"):
                repository.put(
                    "gemini/account",
                    "conversation",
                    remote,
                    {"messages": [{"resources": [{"path": relative}]}]},
                )
            repository.delete("gemini/account", "conversation", "first")
            self.assertEqual(files.collect(repository.file_references()), [])
            repository.delete("gemini/account", "conversation", "second")
            self.assertEqual(files.collect(repository.file_references()), [relative])
            repository.close()

    def test_failed_content_fsync_does_not_publish_a_partial_archive_file(self):
        with tempfile.TemporaryDirectory() as folder:
            files = Files(Path(folder))
            with patch(
                "extensions.archive.repository.os.fsync", side_effect=OSError("disk failure")
            ):
                with self.assertRaises(OSError):
                    files.save(b"not durable")
            self.assertEqual([path for path in files.root.rglob("*") if path.is_file()], [])

    def test_streaming_file_save_uses_bounded_reads_and_deduplicates_content(self):
        class BoundedReader(io.BytesIO):
            def read(self, size=-1):
                if size <= 0 or size > 64 * 1024:
                    raise AssertionError("archive read exceeded its bounded chunk size")
                return super().read(size)

        data = b"attachment" * (96 * 1024)
        with tempfile.TemporaryDirectory() as folder:
            files = Files(Path(folder) / "files")

            relative, digest = files.save_stream(BoundedReader(data))
            duplicate, duplicate_digest = files.save_stream(BoundedReader(data))

            self.assertEqual(relative, duplicate)
            self.assertEqual(digest, hashlib.sha256(data).hexdigest())
            self.assertEqual(duplicate_digest, digest)
            self.assertEqual((files.root / relative).read_bytes(), data)
            self.assertEqual(
                sum(path.is_file() for path in files.root.rglob("*")),
                1,
            )

    def test_gc_preserves_shared_references_recent_saves_and_unknown_files(self):
        import os

        with tempfile.TemporaryDirectory() as folder:
            files = Files(Path(folder))
            live, _ = files.save(b"shared")
            orphan, _ = files.save(b"orphan")
            recent, _ = files.save(b"recent")
            for item in (live, orphan):
                os.utime(files.root / item, (time.time() - 7200,) * 2)
            (files.root / "notes.txt").write_text("keep")
            self.assertEqual(files.collect({live}), [orphan])
            self.assertTrue((files.root / orphan).exists())
            self.assertEqual(files.collect({live}, dry_run=False), [orphan])
            self.assertTrue((files.root / live).exists())
            self.assertTrue((files.root / recent).exists())
            self.assertTrue((files.root / "notes.txt").exists())

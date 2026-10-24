"""Archive schema, file retention and maintenance operations."""

import hashlib
import io
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from extensions.archive.repository import Files, SQLiteRepository


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

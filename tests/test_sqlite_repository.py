"""Shared SQLite connection, transaction, and file-safety contracts."""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from utils.sqlite import SQLiteRepositoryBase


class SQLiteRepositoryBaseTests(unittest.TestCase):
    def test_read_transaction_keeps_its_snapshot_while_a_writer_commits(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = SQLiteRepositoryBase(Path(folder) / "store.sqlite3")
            with repository.connect() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE records(value TEXT)")
                connection.execute("INSERT INTO records VALUES('before')")

            with repository.transaction() as reader:
                self.assertEqual(
                    reader.execute("SELECT value FROM records").fetchone(), ("before",)
                )
                with repository.transaction(immediate=True) as writer:
                    writer.execute("UPDATE records SET value='after'")
                self.assertEqual(
                    reader.execute("SELECT value FROM records").fetchone(), ("before",)
                )

            with repository.connect() as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM records").fetchone(), ("after",)
                )

    def test_connection_is_closed_after_each_operation(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = SQLiteRepositoryBase(Path(folder) / "store.sqlite3")
            repository.prepare_private_database()

            with repository.connect() as connection:
                connection.execute("CREATE TABLE records(value TEXT)")

            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def test_transaction_commits_success_and_rolls_back_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = SQLiteRepositoryBase(Path(folder) / "store.sqlite3")
            repository.prepare_private_database()
            with repository.connect() as connection:
                connection.execute("CREATE TABLE records(value TEXT)")

            with repository.transaction(immediate=True) as connection:
                connection.execute("INSERT INTO records VALUES('committed')")

            with self.assertRaisesRegex(RuntimeError, "rollback"):
                with repository.transaction(immediate=True) as connection:
                    connection.execute("INSERT INTO records VALUES('discarded')")
                    raise RuntimeError("rollback")

            with repository.connect() as connection:
                values = connection.execute("SELECT value FROM records").fetchall()
            self.assertEqual(values, [("committed",)])

    def test_sqlite_failures_are_translated_only_when_configured(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "missing" / "store.sqlite3"
            translated = SQLiteRepositoryBase(path, error_message="storage unavailable")
            with self.assertRaisesRegex(OSError, "storage unavailable") as raised:
                with translated.connect():
                    pass
            self.assertIsInstance(raised.exception.__cause__, sqlite3.Error)

            translated.prepare_private_database()
            with self.assertRaisesRegex(OSError, "storage unavailable") as raised:
                with translated.connect() as connection:
                    connection.execute("SELECT * FROM missing_table")
            self.assertIsInstance(raised.exception.__cause__, sqlite3.Error)

            raw_errors = SQLiteRepositoryBase(path)
            with self.assertRaises(sqlite3.OperationalError):
                with raw_errors.connect() as connection:
                    connection.execute("SELECT * FROM missing_table")

    @unittest.skipUnless(os.name == "posix", "POSIX file permissions")
    def test_new_database_and_parent_directory_are_private(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "private" / "store.sqlite3"
            repository = SQLiteRepositoryBase(path)
            repository.prepare_private_database()

            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()

"""Account SQL isolation, restart, concurrency, permissions and provider storage injection."""

import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from account_repository import AccountRepository
from providers.errors import AccountPaused
from providers.session import delete_session, read_session, save_session, session_exists


class RepositoryTests(unittest.TestCase):
    def test_v1_database_upgrades_without_changing_existing_account_records(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "accounts.sqlite3"
            with closing(sqlite3.connect(path)) as db:
                db.execute(
                    """CREATE TABLE account_records (
                        account_key TEXT NOT NULL,
                        kind TEXT NOT NULL CHECK(kind IN ('credentials','catalog','verification')),
                        updated REAL NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY(account_key,kind)
                    ) WITHOUT ROWID"""
                )
                db.execute(
                    "INSERT INTO account_records VALUES(?,?,?,?)",
                    ("gemini/personal", "catalog", 1.0, '{"models":[]}'),
                )
                db.execute("PRAGMA user_version=1")
                db.commit()

            repository = AccountRepository(path)
            self.assertEqual(repository.get("gemini/personal", "catalog"), {"models": []})
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertIsNotNone(
                    db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_cache'"
                    ).fetchone()
                )

    def test_sqlite_cache_keeps_pointer_conversation_and_tombstone_account_scoped(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "accounts.sqlite3"
            repository = AccountRepository(path)
            record = {"id": "@gemini/a/chat", "messages": [{"text": "cached"}]}
            self.assertTrue(repository.cache_put("gemini/a", "conversation", "chat", record))
            repository.cache_set_active("gemini/a", "chat")
            self.assertEqual(repository.cache_get("gemini/a", "conversation", "chat"), record)
            self.assertEqual(repository.cache_active("gemini/a"), "chat")

            repository.cache_mark_deleted("gemini/a", "chat")
            self.assertTrue(repository.cache_is_deleted("gemini/a", "chat"))
            self.assertIsNone(repository.cache_get("gemini/a", "conversation", "chat"))
            self.assertIsNone(repository.cache_active("gemini/a"))
            self.assertFalse(repository.cache_put("gemini/a", "conversation", "chat", record))
            self.assertIsNone(repository.cache_get("gemini/b", "conversation", "chat"))

    def test_account_cache_pages_are_bounded_and_resume_by_remote_key(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = AccountRepository(Path(folder) / "accounts.sqlite3")
            for remote in ("chat-a", "chat-b", "chat-c"):
                repository.cache_put("claude/work", "conversation", remote, {"id": remote})

            first = repository.cache_records_page("claude/work", "conversation", limit=2)
            second = repository.cache_records_page(
                "claude/work", "conversation", after=first[-1][0], limit=2
            )

            self.assertEqual([remote for remote, _ in first], ["chat-a", "chat-b"])
            self.assertEqual([remote for remote, _ in second], ["chat-c"])
            with self.assertRaises(ValueError):
                repository.cache_records_page("claude/work", "conversation", limit=0)

    def test_credentials_are_account_isolated_and_survive_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "accounts.sqlite3"
            repository = AccountRepository(path)
            first, second = repository.credentials("gemini/a"), repository.credentials("claude/a")
            save_session(first, {"cookie": "fixture"})
            self.assertTrue(session_exists(first))
            self.assertFalse(session_exists(second))
            self.assertEqual(
                read_session(AccountRepository(path).credentials("gemini/a")), {"cookie": "fixture"}
            )
            repository.put("gemini/a", "catalog", {"models": []})
            delete_session(first)
            self.assertEqual(repository.get("gemini/a", "catalog"), {"models": []})
            with self.assertRaises(AccountPaused):
                read_session(first)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_concurrent_accounts_and_delete_do_not_cross_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = AccountRepository(Path(folder) / "accounts.sqlite3")

            def write(i):
                key = "claude/" + str(i)
                repository.put(key, "catalog", {"index": i})
                self.assertEqual(repository.get(key, "catalog"), {"index": i})

            with ThreadPoolExecutor(6) as pool:
                list(pool.map(write, range(30)))
            repository.delete("claude/0")
            self.assertIsNone(repository.get("claude/0", "catalog"))
            self.assertEqual(repository.get("claude/29", "catalog"), {"index": 29})

    def test_unknown_schema_and_oversized_records_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "accounts.sqlite3"
            repository = AccountRepository(path)
            with self.assertRaises(ValueError):
                repository.put("gemini/a", "credentials", {"oversized": "x" * (2 * 1024 * 1024)})
            with closing(sqlite3.connect(path)) as db:
                db.execute("PRAGMA user_version=99")
            with self.assertRaises(ValueError):
                AccountRepository(path)

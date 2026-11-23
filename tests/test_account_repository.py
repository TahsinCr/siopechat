"""Account SQL isolation, restart, concurrency, permissions and CredentialStore contract."""

import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from webaiprovider import AccountPaused
from webaiprovider.accounts import delete_session, read_session, save_session, session_exists

from accounts.repository import AccountRepository


class RepositoryTests(unittest.TestCase):
    def test_repository_initializes_schema_v2_and_serializes_catalog(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "accounts.sqlite3"
            repository = AccountRepository(path)
            repository.put("gemini/personal", "catalog", {"models": []})
            self.assertEqual(repository.get("gemini/personal", "catalog"), {"models": []})
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertIsNotNone(
                    db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_records'"
                    ).fetchone()
                )
            reopened = AccountRepository(path)
            self.assertEqual(reopened.get("gemini/personal", "catalog"), {"models": []})

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

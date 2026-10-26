"""Private account metadata and credential persistence, outside the provider library."""

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class AccountRepository:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Create privately before SQLite opens it; an existing database is hardened too.
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        if os.name != "nt":
            self.path.chmod(0o600)
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise ValueError("Unsupported account database schema")
            if version < 2:
                db.execute("BEGIN IMMEDIATE")
                if version == 0:
                    db.execute(
                        """CREATE TABLE IF NOT EXISTS account_records (
                            account_key TEXT NOT NULL,
                            kind TEXT NOT NULL CHECK(kind IN ('credentials','catalog','verification')),
                            updated REAL NOT NULL,
                            payload TEXT NOT NULL,
                            PRIMARY KEY(account_key,kind)
                        ) WITHOUT ROWID"""
                    )
                db.execute(
                    """CREATE TABLE IF NOT EXISTS account_cache (
                        account_key TEXT NOT NULL,
                        kind TEXT NOT NULL CHECK(kind IN ('conversation','pointer','tombstone')),
                        remote TEXT NOT NULL,
                        updated REAL NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY(account_key,kind,remote)
                    ) WITHOUT ROWID"""
                )
                db.execute("PRAGMA user_version=2")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA secure_delete=ON")
            with db:
                yield db
        except sqlite3.Error as error:
            raise OSError("Account database operation failed") from error
        finally:
            db.close()

    def get(self, key, kind):
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM account_records WHERE account_key=? AND kind=?", (key, kind)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, kind, value):
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode()) > 2 * 1024 * 1024:
            raise ValueError("Account record exceeds size limit")
        with self.connect() as db:
            db.execute(
                "INSERT INTO account_records VALUES(?,?,?,?) ON CONFLICT(account_key,kind) DO UPDATE SET updated=excluded.updated,payload=excluded.payload",
                (key, kind, time.time(), payload),
            )

    def delete(self, key, kind=None):
        with self.connect() as db:
            if kind:
                db.execute(
                    "DELETE FROM account_records WHERE account_key=? AND kind=?", (key, kind)
                )
            else:
                db.execute("DELETE FROM account_records WHERE account_key=?", (key,))

    def cache_get(self, key, kind, remote):
        if kind not in {"conversation", "pointer", "tombstone"}:
            raise ValueError("Unsupported account cache record")
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM account_cache WHERE account_key=? AND kind=? AND remote=?",
                (key, kind, remote),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def cache_exists(self, key, kind, remote):
        if kind not in {"conversation", "pointer", "tombstone"}:
            raise ValueError("Unsupported account cache record")
        with self.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM account_cache WHERE account_key=? AND kind=? AND remote=?",
                    (key, kind, remote),
                ).fetchone()
                is not None
            )

    def cache_records(self, key, kind):
        if kind not in {"conversation", "pointer", "tombstone"}:
            raise ValueError("Unsupported account cache record")
        with self.connect() as db:
            rows = db.execute(
                "SELECT remote,payload FROM account_cache WHERE account_key=? AND kind=?",
                (key, kind),
            ).fetchall()
        return [(remote, json.loads(payload)) for remote, payload in rows]

    def cache_pending_branches(self, key):
        with self.connect() as db:
            rows = db.execute(
                "SELECT remote,payload FROM account_cache "
                "WHERE account_key=? AND kind='conversation' "
                "AND payload LIKE '%\"pending_local_branch\":true%'",
                (key,),
            ).fetchall()
        return [
            (remote, value)
            for remote, payload in rows
            if (value := json.loads(payload)).get("pending_local_branch")
        ]

    def cache_records_page(self, key, kind, *, after="", limit=16):
        if kind not in {"conversation", "pointer", "tombstone"}:
            raise ValueError("Unsupported account cache record")
        if not isinstance(after, str) or not 1 <= limit <= 256:
            raise ValueError("Invalid account cache page")
        with self.connect() as db:
            rows = db.execute(
                "SELECT remote,payload FROM account_cache "
                "WHERE account_key=? AND kind=? AND remote>? ORDER BY remote LIMIT ?",
                (key, kind, after, limit),
            ).fetchall()
        return [(remote, json.loads(payload)) for remote, payload in rows]

    def cache_put(self, key, kind, remote, value):
        if kind not in {"conversation", "pointer", "tombstone"}:
            raise ValueError("Unsupported account cache record")
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self.connect() as db:
            if (
                kind == "conversation"
                and db.execute(
                    "SELECT 1 FROM account_cache WHERE account_key=? AND kind='tombstone' AND remote=?",
                    (key, remote),
                ).fetchone()
            ):
                return False
            db.execute(
                """INSERT INTO account_cache VALUES(?,?,?,?,?)
                ON CONFLICT(account_key,kind,remote) DO UPDATE SET
                updated=excluded.updated,payload=excluded.payload""",
                (key, kind, remote, time.time(), payload),
            )
        return True

    def cache_delete(self, key, kind, remote):
        if kind not in {"conversation", "pointer", "tombstone"}:
            raise ValueError("Unsupported account cache record")
        with self.connect() as db:
            db.execute(
                "DELETE FROM account_cache WHERE account_key=? AND kind=? AND remote=?",
                (key, kind, remote),
            )

    def cache_is_deleted(self, key, remote):
        return self.cache_get(key, "tombstone", remote) is not None

    def cache_set_active(self, key, remote):
        if remote is None:
            self.cache_delete(key, "pointer", "active")
        else:
            self.cache_put(key, "pointer", "active", {"remote": remote})

    def cache_active(self, key):
        value = self.cache_get(key, "pointer", "active")
        remote = value.get("remote") if isinstance(value, dict) else None
        return remote if isinstance(remote, str) and remote else None

    def cache_mark_deleted(self, key, remote):
        with self.connect() as db:
            db.execute(
                """INSERT INTO account_cache VALUES(?,?,?,?,?)
                ON CONFLICT(account_key,kind,remote) DO UPDATE SET
                updated=excluded.updated,payload=excluded.payload""",
                (key, "tombstone", remote, time.time(), "{}"),
            )
            db.execute(
                "DELETE FROM account_cache WHERE account_key=? AND kind='conversation' AND remote=?",
                (key, remote),
            )
            row = db.execute(
                "SELECT payload FROM account_cache WHERE account_key=? AND kind='pointer' AND remote='active'",
                (key,),
            ).fetchone()
            active = json.loads(row[0]) if row else None
            if isinstance(active, dict) and active.get("remote") == remote:
                db.execute(
                    "DELETE FROM account_cache WHERE account_key=? AND kind='pointer' AND remote='active'",
                    (key,),
                )

    def credentials(self, key):
        return AccountCredentials(self, key)


class AccountCredentials:
    """Implement the provider library's credential storage contract."""

    def __init__(self, repository, key):
        self.repository, self.key = repository, key

    def load(self):
        value = self.repository.get(self.key, "credentials")
        if value is None:
            raise FileNotFoundError("No saved account session")
        return value

    def save(self, value):
        self.repository.put(self.key, "credentials", value)

    def exists(self):
        return self.repository.get(self.key, "credentials") is not None

    def delete(self):
        self.repository.delete(self.key, "credentials")

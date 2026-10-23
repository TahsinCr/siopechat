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
            if version not in (0, 1):
                raise ValueError("Unsupported account database schema")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS account_records (
                    account_key TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('credentials','catalog','verification')),
                    updated REAL NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(account_key,kind)
                ) WITHOUT ROWID;
                PRAGMA user_version=1;
            """)

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

"""Private account metadata and credential persistence, outside the provider library."""

from __future__ import annotations

import json
import time

from webaiprovider import CredentialStore

from utils.sqlite import SQLiteRepositoryBase


class AccountRepository(SQLiteRepositoryBase):
    """Persist account credentials, cached model catalogs, and verification markers."""

    def __init__(self, path):
        super().__init__(path, timeout=5, error_message="Account database operation failed")
        self.prepare_private_database()
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 2):
            raise ValueError("Unsupported account database schema")
        if version == 0:
            with self.transaction(immediate=True) as db:
                if db.execute("PRAGMA user_version").fetchone()[0] == 0:
                    db.execute(
                        """CREATE TABLE IF NOT EXISTS account_records (
                            account_key TEXT NOT NULL,
                            kind TEXT NOT NULL CHECK(kind IN ('credentials','catalog','verification')),
                            updated REAL NOT NULL,
                            payload TEXT NOT NULL,
                            PRIMARY KEY(account_key,kind)
                        ) WITHOUT ROWID"""
                    )
                    db.execute("PRAGMA user_version=2")

    def configure_connection(self, connection):
        connection.execute("PRAGMA secure_delete=ON")

    def get(self, key: str, kind: str):
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM account_records WHERE account_key=? AND kind=?", (key, kind)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, kind: str, value) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode()) > 2 * 1024 * 1024:
            raise ValueError("Account record exceeds size limit")
        with self.connect() as db:
            db.execute(
                "INSERT INTO account_records VALUES(?,?,?,?) "
                "ON CONFLICT(account_key,kind) DO UPDATE SET "
                "updated=excluded.updated,payload=excluded.payload",
                (key, kind, time.time(), payload),
            )

    def delete(self, key: str, kind: str | None = None) -> None:
        with self.connect() as db:
            if kind:
                db.execute(
                    "DELETE FROM account_records WHERE account_key=? AND kind=?", (key, kind)
                )
            else:
                db.execute("DELETE FROM account_records WHERE account_key=?", (key,))

    def credentials(self, key: str) -> "AccountCredentials":
        return AccountCredentials(self, key)


class AccountCredentials(CredentialStore):
    """Implement the provider library's CredentialStore contract."""

    def __init__(self, repository: AccountRepository, key: str):
        self.repository = repository
        self.key = key

    def load(self):
        value = self.repository.get(self.key, "credentials")
        if value is None:
            raise FileNotFoundError("No saved account session")
        return value

    def save(self, value) -> None:
        self.repository.put(self.key, "credentials", value)

    def exists(self) -> bool:
        return self.repository.get(self.key, "credentials") is not None

    def delete(self) -> None:
        self.repository.delete(self.key, "credentials")

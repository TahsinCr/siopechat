"""Persistent download ledger, independent of terminal presentation."""

import json

from .repository import encode


class DownloadHistory:
    def __init__(self, repository):
        self.repository = repository

    def list(self):
        with self.repository._connect() as db:
            rows = db.execute("SELECT payload FROM downloads ORDER BY created DESC, id").fetchall()
        return [json.loads(row[0]) for row in rows]

    def put(self, record):
        with self.repository._connect() as db:
            db.execute(
                "INSERT INTO downloads VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "payload=excluded.payload",
                (record["id"], record["created"], encode(record)),
            )

    def remove(self, identity):
        with self.repository._connect() as db:
            db.execute("DELETE FROM downloads WHERE id=?", (identity,))

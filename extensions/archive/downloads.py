"""Persistent download ledger, independent of terminal presentation."""

import json

from .repository import encode


class DownloadHistory:
    def __init__(self, repository):
        self.repository = repository

    def list(self):
        with self.repository._connect() as db:
            rows = db.execute(
                "SELECT revision, payload FROM downloads ORDER BY created DESC, id"
            ).fetchall()
        records = []
        for revision, payload in rows:
            record = json.loads(payload)
            record["_revision"] = revision
            records.append(record)
        return records

    def put_many(self, records):
        """Persist newer rows in one transaction; return conflicting DB copies."""
        stale = {}
        with self.repository._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for record in records:
                revision = max(1, int(record.get("_revision", 1)))
                payload_record = dict(record)
                payload_record.pop("_revision", None)
                cursor = db.execute(
                    "INSERT INTO downloads(id,created,revision,payload) VALUES(?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET created=excluded.created, "
                    "revision=excluded.revision, payload=excluded.payload "
                    "WHERE excluded.revision > downloads.revision",
                    (record["id"], record["created"], revision, encode(payload_record)),
                )
                if cursor.rowcount == 0:
                    row = db.execute(
                        "SELECT revision,payload FROM downloads WHERE id=?", (record["id"],)
                    ).fetchone()
                    if row:
                        value = json.loads(row[1])
                        value["_revision"] = row[0]
                        stale[record["id"]] = value
        return stale

    def remove(self, identity):
        with self.repository._connect() as db:
            db.execute("DELETE FROM downloads WHERE id=?", (identity,))

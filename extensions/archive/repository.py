"""Versioned SQLite records; binary content lives in a separate file store."""

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def conversation_header(value):
    """Listing/window headers must not retain provider-native copies of the body."""
    return {
        key: item
        for key, item in value.items()
        if key not in ("messages", "metadata", "_archive_sql_messages", "_archive_sql_metadata")
    }


def _is_archive_blob_path(value):
    if not isinstance(value, str):
        return False
    parts = value.replace("\\", "/").split("/")
    return (
        len(parts) == 2
        and len(parts[0]) == 2
        and len(parts[1]) == 64
        and parts[0] == parts[1][:2]
        and all(char in "0123456789abcdef" for char in parts[1])
    )


def _strip_archive_blob_refs(value):
    changed = False
    if isinstance(value, dict):
        looks_like_resource = "id" in value and any(
            name in value for name in ("name", "filename", "mime", "mime_type")
        )
        if _is_archive_blob_path(value.get("path")) or (
            looks_like_resource and ("path" in value or "sha256" in value)
        ):
            value.pop("path", None)
            value.pop("sha256", None)
            changed = True
        for item in value.values():
            changed = _strip_archive_blob_refs(item) or changed
    elif isinstance(value, list):
        for item in value:
            changed = _strip_archive_blob_refs(item) or changed
    return changed


class Repository(Protocol):
    def get(self, account, kind, remote): ...
    def put(self, account, kind, remote, value): ...
    def state(self, account, kind, remote, state, error=None): ...
    def conversations(self, account, limit=50, offset=0): ...
    def containers(self, account, limit=50, offset=0): ...
    def messages(self, account, remote, limit=100, offset=0): ...
    def conversation_window(self, account, remote, *, start=None, limit=80, max_chars=262144): ...
    def resources(self, account, owner_kind, owner): ...
    def tombstones(self): ...
    def delete(self, account, kind, remote): ...
    def close(self): ...


class SQLiteRepository:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as db:
            exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='schema_version'"
            ).fetchone()
            if exists:
                versions = db.execute("SELECT version FROM schema_version").fetchall()
                if versions not in ([(1,)], [(2,)], [(3,)], [(4,)], [(5,)]):
                    raise OSError("Unsupported archive schema version")
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
                INSERT INTO schema_version SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM schema_version);
                CREATE TABLE IF NOT EXISTS tombstones(
                    account TEXT NOT NULL, kind TEXT NOT NULL, remote TEXT NOT NULL,
                    deleted REAL NOT NULL, PRIMARY KEY(account,kind,remote));
                CREATE TABLE IF NOT EXISTS records(
                    account TEXT NOT NULL, kind TEXT NOT NULL, remote TEXT NOT NULL,
                    provider TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
                    changed REAL NOT NULL, checked REAL NOT NULL,
                    PRIMARY KEY(account,kind,remote));
                CREATE TABLE IF NOT EXISTS conversations(
                    account TEXT NOT NULL, remote TEXT NOT NULL, title TEXT, container TEXT,
                    model TEXT, metadata TEXT NOT NULL, PRIMARY KEY(account,remote));
                CREATE INDEX IF NOT EXISTS conversation_container ON conversations(account,container);
                CREATE TABLE IF NOT EXISTS messages(
                    account TEXT NOT NULL, conversation TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    remote TEXT, role TEXT, channel TEXT, text TEXT, metadata TEXT NOT NULL,
                    PRIMARY KEY(account,conversation,ordinal));
                CREATE INDEX IF NOT EXISTS message_origin ON messages(account,conversation,remote);
                CREATE TABLE IF NOT EXISTS containers(
                    account TEXT NOT NULL, remote TEXT NOT NULL, kind TEXT NOT NULL,
                    name TEXT, instructions TEXT, metadata TEXT NOT NULL,
                    PRIMARY KEY(account,remote));
                CREATE TABLE IF NOT EXISTS memberships(
                    account TEXT NOT NULL, conversation TEXT NOT NULL, container TEXT NOT NULL,
                    PRIMARY KEY(account,conversation,container));
                CREATE INDEX IF NOT EXISTS container_members ON memberships(account,container);
                CREATE TABLE IF NOT EXISTS resources(
                    account TEXT NOT NULL, owner_kind TEXT NOT NULL, owner TEXT NOT NULL,
                    ordinal INTEGER NOT NULL, remote TEXT, kind TEXT, name TEXT, mime TEXT,
                    uri TEXT, path TEXT, metadata TEXT NOT NULL,
                    PRIMARY KEY(account,owner_kind,owner,ordinal));
                CREATE TABLE IF NOT EXISTS synchronization(
                    account TEXT NOT NULL, kind TEXT NOT NULL, remote TEXT NOT NULL,
                    state TEXT NOT NULL, checked REAL NOT NULL, error TEXT,
                    PRIMARY KEY(account,kind,remote));
                CREATE TABLE IF NOT EXISTS maintenance(
                    key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
        # Upgrade the existing archive atomically; conversation tables remain intact.
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS downloads("
                "id TEXT PRIMARY KEY, created REAL NOT NULL, payload TEXT NOT NULL)"
            )
            db.execute("UPDATE schema_version SET version=2 WHERE version=1")
        # Revisions let a process reject stale download-history writes without
        # changing the payload contract or overwriting another process's row.
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(downloads)")}
            if "revision" not in columns:
                db.execute("ALTER TABLE downloads ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
            db.execute("UPDATE schema_version SET version=3 WHERE version=2")
        # Older versions duplicated complete histories in the listing metadata.
        # Migrate one record at a time; the canonical record and messages stay intact.
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS conversation_metadata("
                "account TEXT NOT NULL, remote TEXT NOT NULL, payload TEXT NOT NULL,"
                "PRIMARY KEY(account,remote))"
            )
            if db.execute("SELECT version FROM schema_version").fetchone()[0] == 3:
                for account, remote, metadata in db.execute(
                    "SELECT account,remote,metadata FROM conversations"
                ):
                    header = json.loads(metadata)
                    if "messages" in header:
                        header = conversation_header(header)
                        db.execute(
                            "UPDATE conversations SET metadata=? WHERE account=? AND remote=?",
                            (encode(header), account, remote),
                        )
                for account, remote, payload in db.execute(
                    "SELECT account,remote,payload FROM records WHERE kind='conversation'"
                ):
                    value = json.loads(payload)
                    changed = False
                    if "messages" in value:
                        messages = value.pop("messages")
                        count = db.execute(
                            "SELECT count(*) FROM messages WHERE account=? AND conversation=?",
                            (account, remote),
                        ).fetchone()[0]
                        if count != len(messages):
                            self._write_messages(db, account, remote, messages)
                        value["_archive_sql_messages"] = True
                        changed = True
                    if "metadata" in value:
                        db.execute(
                            "INSERT OR REPLACE INTO conversation_metadata VALUES(?,?,?)",
                            (account, remote, encode(value.pop("metadata"))),
                        )
                        value["_archive_sql_metadata"] = True
                        changed = True
                    if changed:
                        db.execute(
                            "UPDATE records SET payload=? WHERE account=? "
                            "AND kind='conversation' AND remote=?",
                            (encode(value), account, remote),
                        )
                db.execute("UPDATE schema_version SET version=4 WHERE version=3")
        # v5 stops duplicating uploaded source bytes in the archive. Remove only
        # content-addressed archive paths; provider URIs and attachment metadata stay.
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("SELECT version FROM schema_version").fetchone()[0]
            if version == 4:
                for table, column in (
                    ("records", "payload"),
                    ("conversations", "metadata"),
                    ("messages", "metadata"),
                    ("containers", "metadata"),
                    ("conversation_metadata", "payload"),
                ):
                    rows = db.execute(f"SELECT rowid,{column} FROM {table}").fetchall()
                    for rowid, payload in rows:
                        value = json.loads(payload)
                        if _strip_archive_blob_refs(value):
                            db.execute(
                                f"UPDATE {table} SET {column}=? WHERE rowid=?",
                                (encode(value), rowid),
                            )
                for account, owner_kind, owner, ordinal, path, metadata in db.execute(
                    "SELECT account,owner_kind,owner,ordinal,path,metadata FROM resources"
                ).fetchall():
                    value = json.loads(metadata)
                    changed = _strip_archive_blob_refs(value)
                    remove_path = path is not None
                    if changed or remove_path:
                        db.execute(
                            "UPDATE resources SET path=? ,metadata=? WHERE account=? "
                            "AND owner_kind=? AND owner=? AND ordinal=?",
                            (
                                None if remove_path else path,
                                encode(value) if changed else metadata,
                                account,
                                owner_kind,
                                owner,
                                ordinal,
                            ),
                        )
                db.execute(
                    "INSERT OR REPLACE INTO maintenance(key,value) "
                    "VALUES('uploaded_file_cleanup_pending','1')"
                )
                db.execute("UPDATE schema_version SET version=5 WHERE version=4")
        if os.name != "nt":
            self.path.chmod(0o600)

    @contextmanager
    def _connect(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=5)
            db.execute("PRAGMA busy_timeout=5000")
            with db:
                yield db
        except sqlite3.Error as error:
            raise OSError("Archive SQLite storage is unavailable") from error
        finally:
            if db is not None:
                db.close()

    def get(self, account, kind, remote):
        with self._connect() as db:
            db.execute("BEGIN")
            row = db.execute(
                "SELECT payload FROM records WHERE account=? AND kind=? AND remote=?",
                (account, kind, remote),
            ).fetchone()
            value = json.loads(row[0]) if row else None
            if value and value.pop("_archive_sql_metadata", False):
                metadata = db.execute(
                    "SELECT payload FROM conversation_metadata WHERE account=? AND remote=?",
                    (account, remote),
                ).fetchone()
                value["metadata"] = json.loads(metadata[0]) if metadata else {}
            if value and value.pop("_archive_sql_messages", False):
                value["messages"] = [
                    json.loads(item[0])
                    for item in db.execute(
                        "SELECT metadata FROM messages WHERE account=? AND conversation=? "
                        "ORDER BY ordinal",
                        (account, remote),
                    )
                ]
            return value

    def put(self, account, kind, remote, value):
        payload = encode(value)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        if kind == "conversation":
            header = {k: v for k, v in value.items() if k not in ("messages", "metadata")}
            if "messages" in value:
                header["_archive_sql_messages"] = True
            if "metadata" in value:
                header["_archive_sql_metadata"] = True
            payload = encode(header)
        now = time.time()
        with self._connect() as db:
            prior = db.execute(
                "SELECT digest FROM records WHERE account=? AND kind=? AND remote=?",
                (account, kind, remote),
            ).fetchone()
            if prior and prior[0] == digest:
                db.execute(
                    "UPDATE records SET checked=? WHERE account=? AND kind=? AND remote=?",
                    (now, account, kind, remote),
                )
                return False
            db.execute(
                """INSERT INTO records VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(account,kind,remote) DO UPDATE SET
                payload=excluded.payload,digest=excluded.digest,changed=excluded.changed,
                checked=excluded.checked""",
                (account, kind, remote, account.split("/")[0], payload, digest, now, now),
            )
            conversations = (
                [value]
                if kind == "conversation"
                else value.get("items", [])
                if kind in ("chats", "container_chats")
                else []
            )
            for item in conversations:
                item_id = item.get("remote_id") or item.get("id") or remote
                prefix = "@" + account + "/"
                if item_id.startswith(prefix):
                    item_id = unquote(item_id[len(prefix) :])
                db.execute(
                    "INSERT OR REPLACE INTO conversations VALUES(?,?,?,?,?,?)",
                    (
                        account,
                        item_id,
                        item.get("title"),
                        item.get("project_id"),
                        item.get("model"),
                        encode(conversation_header(item) if kind == "conversation" else item),
                    ),
                )
            if kind == "containers":
                for item in value.get("items", []):
                    db.execute(
                        "INSERT INTO containers VALUES(?,?,?,?,?,?)\n                        ON CONFLICT(account,remote) DO UPDATE SET name=excluded.name,\n                        metadata=excluded.metadata",
                        (
                            account,
                            self._remote(account, item.get("remote_id") or item.get("id")),
                            item.get("kind", "project"),
                            item.get("name"),
                            item.get("instructions"),
                            encode(item),
                        ),
                    )
                    if item.get("resources") or item.get("kind") == "notebook":
                        self._write_resources(
                            db,
                            account,
                            "container",
                            self._remote(account, item.get("remote_id") or item.get("id")),
                            item,
                        )
            if kind == "conversation":
                db.execute(
                    "DELETE FROM conversation_metadata WHERE account=? AND remote=?",
                    (account, remote),
                )
                if "metadata" in value:
                    db.execute(
                        "INSERT INTO conversation_metadata VALUES(?,?,?)",
                        (account, remote, encode(value["metadata"])),
                    )
                self._write_messages(db, account, remote, value.get("messages", []))
                db.execute(
                    "DELETE FROM memberships WHERE account=? AND conversation=?", (account, remote)
                )
                if value.get("project_id"):
                    db.execute(
                        "INSERT INTO memberships VALUES(?,?,?)",
                        (account, remote, self._remote(account, value["project_id"])),
                    )
            if kind == "container":
                db.execute(
                    """INSERT OR REPLACE INTO containers VALUES(?,?,?,?,?,?)""",
                    (
                        account,
                        remote,
                        value.get("kind", "project"),
                        value.get("name"),
                        value.get("instructions"),
                        encode(value),
                    ),
                )
            if kind in ("conversation", "container"):
                self._write_resources(db, account, kind, remote, value)
        return True

    @staticmethod
    def _write_messages(db, account, remote, messages, *, start=0):
        db.execute(
            "DELETE FROM messages WHERE account=? AND conversation=? AND ordinal>=?",
            (account, remote, start),
        )
        db.executemany(
            "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)",
            (
                (
                    account,
                    remote,
                    start + index,
                    message.get("id"),
                    message.get("role"),
                    message.get("channel"),
                    message.get("text", ""),
                    encode(message),
                )
                for index, message in enumerate(messages)
            ),
        )

    def append_turn(self, account, remote, value):
        """Reconcile only the bounded tail, never decode/rewrite the complete history."""
        from .history import merge_conversation

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM records WHERE account=? AND kind='conversation' AND remote=?",
                (account, remote),
            ).fetchone()
            current = json.loads(row[0]) if row else {}
            base_end = value.get("_archive_base_end")
            tail = list(
                db.execute(
                    "SELECT ordinal,metadata FROM messages WHERE account=? AND conversation=? "
                    "AND ordinal>=? ORDER BY ordinal DESC LIMIT 32",
                    (account, remote, base_end or 0),
                )
            )
            tail.reverse()
            last = db.execute(
                "SELECT ordinal FROM messages WHERE account=? AND conversation=? "
                "ORDER BY ordinal DESC LIMIT 1",
                (account, remote),
            ).fetchone()
            tail_values = [json.loads(item[1]) for item in tail]
            new_ids = {item.get("id") for item in value.get("messages", ()) if item.get("id")}
            old_ids = {item.get("id") for item in tail_values if item.get("id")}
            if (
                tail_values
                and tail_values[-1].get("local_pending")
                and new_ids
                and new_ids.isdisjoint(old_ids)
            ):
                # Identical text in two separately accepted sends is still two turns.
                # Remote-confirmed tails can instead be reconciled by content.
                tail, tail_values = [], []
            first = tail[0][0] if tail else last[0] + 1 if last else 0
            merged = merge_conversation(dict(current, messages=tail_values), value)
            messages = merged.pop("messages", [])
            header = dict(
                current,
                **{k: v for k, v in value.items() if k not in ("messages", "_archive_base_end")},
            )
            if "metadata" in header:
                db.execute(
                    "INSERT OR REPLACE INTO conversation_metadata VALUES(?,?,?)",
                    (account, remote, encode(header.pop("metadata"))),
                )
                header["_archive_sql_metadata"] = True
            header["_archive_sql_messages"] = True
            payload = encode(header)
            now = time.time()
            digest = hashlib.sha256((payload + encode(messages)).encode()).hexdigest()
            db.execute(
                "INSERT INTO records VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(account,kind,remote) DO UPDATE SET payload=excluded.payload,"
                "digest=excluded.digest,changed=excluded.changed,checked=excluded.checked",
                (account, "conversation", remote, account.split("/")[0], payload, digest, now, now),
            )
            self._write_messages(db, account, remote, messages, start=first)
            db.execute(
                "INSERT OR REPLACE INTO conversations VALUES(?,?,?,?,?,?)",
                (
                    account,
                    remote,
                    header.get("title"),
                    header.get("project_id"),
                    header.get("model"),
                    encode(
                        conversation_header(
                            {k: v for k, v in header.items() if k != "_archive_sql_messages"}
                        )
                    ),
                ),
            )
            db.execute(
                "DELETE FROM memberships WHERE account=? AND conversation=?", (account, remote)
            )
            if header.get("project_id"):
                db.execute(
                    "INSERT OR IGNORE INTO memberships VALUES(?,?,?)",
                    (account, remote, self._remote(account, header["project_id"])),
                )
            self._write_resources(db, account, "conversation", remote, value, append=True)

    def container_members(self, account, remote):
        with self._connect() as db:
            values = {
                row[0]
                for row in db.execute(
                    "SELECT conversation FROM memberships WHERE account=? AND container=?",
                    (account, remote),
                )
            }
            values.update(
                row[0]
                for row in db.execute(
                    "SELECT remote,container FROM conversations WHERE account=?", (account,)
                )
                if self._remote(account, row[1]) == remote
            )
        return values

    def tombstones(self):
        with self._connect() as db:
            return set(db.execute("SELECT account,kind,remote FROM tombstones"))

    def delete(self, account, kind, remote, *, children=None):
        """Remove normalized rows and every saved page reference in one transaction."""
        removed = [(account, kind, remote)]
        with self._connect() as db:
            if kind == "container":
                members = db.execute(
                    "SELECT conversation FROM memberships WHERE account=? AND container=?",
                    (account, remote),
                ).fetchall()
                removed.extend(
                    (account, "conversation", row[0])
                    for row in members
                    if children is None or row[0] in children
                )
                known = {x[2] for x in removed}
                for chat_remote, container in db.execute(
                    "SELECT remote,container FROM conversations WHERE account=?", (account,)
                ).fetchall():
                    if (
                        container
                        and self._remote(account, container) == remote
                        and chat_remote not in known
                        and (children is None or chat_remote in children)
                    ):
                        removed.append((account, "conversation", chat_remote))
                        known.add(chat_remote)
                db.execute("DELETE FROM containers WHERE account=? AND remote=?", (account, remote))
                db.execute(
                    "DELETE FROM memberships WHERE account=? AND container=?", (account, remote)
                )
                db.execute(
                    "DELETE FROM records WHERE account=? AND kind='container_chats' AND remote LIKE ?",
                    (account, remote + ":%"),
                )
            for _, target_kind, target in removed:
                db.execute(
                    "INSERT OR REPLACE INTO tombstones VALUES(?,?,?,?)",
                    (account, target_kind, target, time.time()),
                )
                db.execute(
                    "DELETE FROM records WHERE account=? AND kind=? AND remote=?",
                    (account, target_kind, target),
                )
                db.execute(
                    "DELETE FROM resources WHERE account=? AND owner_kind=? AND owner=?",
                    (account, target_kind, target),
                )
                if target_kind == "conversation":
                    db.execute(
                        "DELETE FROM conversation_metadata WHERE account=? AND remote=?",
                        (account, target),
                    )
                    db.execute(
                        "DELETE FROM conversations WHERE account=? AND remote=?", (account, target)
                    )
                    db.execute(
                        "DELETE FROM messages WHERE account=? AND conversation=?", (account, target)
                    )
                    db.execute(
                        "DELETE FROM memberships WHERE account=? AND conversation=?",
                        (account, target),
                    )
                db.execute(
                    "DELETE FROM synchronization WHERE account=? AND kind=? AND remote=?",
                    (account, target_kind, target),
                )
            if kind == "container":
                # Only confirmed-missing children are tombstoned. Retain the
                # others as unassigned while their own history is revalidated.
                for child, metadata in db.execute(
                    "SELECT remote,metadata FROM conversations WHERE account=?", (account,)
                ).fetchall():
                    item = json.loads(metadata)
                    if self._remote(account, item.get("project_id")) != remote:
                        continue
                    item["project_id"] = None
                    db.execute(
                        "UPDATE conversations SET container=NULL,metadata=? WHERE account=? AND remote=?",
                        (encode(item), account, child),
                    )
                    row = db.execute(
                        "SELECT payload FROM records WHERE account=? AND kind='conversation' AND remote=?",
                        (account, child),
                    ).fetchone()
                    if row:
                        record = json.loads(row[0])
                        record["project_id"] = None
                        payload = encode(record)
                        db.execute(
                            "UPDATE records SET payload=?,digest=? WHERE account=? AND kind='conversation' AND remote=?",
                            (payload, hashlib.sha256(payload.encode()).hexdigest(), account, child),
                        )
            deleted = {(k, r) for _, k, r in removed}
            for page_kind, page_remote, payload in db.execute(
                "SELECT kind,remote,payload FROM records WHERE account=? AND kind IN ('chats','container_chats','containers','pointer')",
                (account,),
            ).fetchall():
                value = json.loads(payload)
                if page_kind == "pointer":
                    if ("conversation", value.get("remote")) in deleted:
                        value["remote"] = None
                else:
                    target_kind = "container" if page_kind == "containers" else "conversation"
                    value["items"] = [
                        x
                        for x in value.get("items", [])
                        if (target_kind, self._remote(account, x.get("remote_id") or x.get("id")))
                        not in deleted
                    ]
                if kind == "container" and page_kind in ("chats", "container_chats"):
                    for item in value.get("items", []):
                        if self._remote(account, item.get("project_id")) == remote:
                            item["project_id"] = None
                encoded = encode(value)
                db.execute(
                    "UPDATE records SET payload=?,digest=? WHERE account=? AND kind=? AND remote=?",
                    (
                        encoded,
                        hashlib.sha256(encoded.encode()).hexdigest(),
                        account,
                        page_kind,
                        page_remote,
                    ),
                )
        return removed

    def _write_resources(self, db, account, kind, remote, value, *, append=False):
        first = 0
        if append:
            first = db.execute(
                "SELECT COALESCE(MAX(ordinal)+1,0) FROM resources "
                "WHERE account=? AND owner_kind=? AND owner=?",
                (account, kind, remote),
            ).fetchone()[0]
        else:
            db.execute(
                "DELETE FROM resources WHERE account=? AND owner_kind=? AND owner=?",
                (account, kind, remote),
            )
        resources = list(value.get("resources", []))
        for message in value.get("messages", []):
            resources.extend(message.get("resources", []))
            for part in message.get("content", []):
                if part.get("resource"):
                    resources.append(part["resource"])
        for i, r in enumerate(resources, first):
            metadata = encode(r)
            if (
                append
                and db.execute(
                    "SELECT 1 FROM resources WHERE account=? AND owner_kind=? AND owner=? "
                    "AND metadata=? LIMIT 1",
                    (account, kind, remote, metadata),
                ).fetchone()
            ):
                continue
            db.execute(
                "INSERT INTO resources VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    account,
                    kind,
                    remote,
                    i,
                    r.get("id"),
                    r.get("kind"),
                    r.get("name"),
                    r.get("mime_type") or r.get("media_type"),
                    r.get("uri") or r.get("remote_url"),
                    r.get("path"),
                    metadata,
                ),
            )

    def state(self, account, kind, remote, state, error=None):
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO synchronization VALUES(?,?,?,?,?,?)",
                (account, kind, remote, state, time.time(), error),
            )

    @staticmethod
    def _remote(account, value):
        prefix = "@" + account + "/"
        return unquote(value[len(prefix) :]) if value and value.startswith(prefix) else value

    def conversations(self, account, limit=50, offset=0):
        return self._rows("conversations", account, limit, offset)

    def containers(self, account, limit=50, offset=0):
        return self._rows("containers", account, limit, offset)

    def _rows(self, table, account, limit, offset):
        """Stable remote-ID order; presentation sorting is explicitly owned by callers."""
        if not 1 <= limit <= 500 or offset < 0:
            raise ValueError("limit must be 1..500 and offset nonnegative")
        with self._connect() as db:
            rows = db.execute(
                f"SELECT metadata FROM {table} WHERE account=? ORDER BY remote LIMIT ? OFFSET ?",
                (account, limit, offset),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def messages(self, account, remote, limit=100, offset=0):
        if not 1 <= limit <= 500 or offset < 0:
            raise ValueError("invalid message page")
        with self._connect() as db:
            rows = db.execute(
                "SELECT metadata FROM messages WHERE account=? AND conversation=? ORDER BY ordinal LIMIT ? OFFSET ?",
                (account, remote, limit, offset),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def conversation_window(self, account, remote, *, start=None, limit=80, max_chars=262144):
        """Read a bounded history window using the ordinal index, newest first by default.

        A single message is never truncated, even if it exceeds the character budget.
        All queries share a read transaction so a concurrent sync cannot mix revisions.
        """
        if not 1 <= limit <= 500 or max_chars < 1 or (start is not None and start < 0):
            raise ValueError("invalid conversation window")
        with self._connect() as db:
            db.execute("BEGIN")
            row = db.execute(
                "SELECT metadata FROM conversations WHERE account=? AND remote=? "
                "AND EXISTS(SELECT 1 FROM records WHERE account=? AND kind='conversation' "
                "AND remote=?)",
                (account, remote, account, remote),
            ).fetchone()
            if row is None:
                return None
            header = json.loads(row[0])
            last = db.execute(
                "SELECT ordinal FROM messages WHERE account=? AND conversation=? "
                "ORDER BY ordinal DESC LIMIT 1",
                (account, remote),
            ).fetchone()
            total = last[0] + 1 if last else 0
            tail = start is None or start >= total
            start = min(start or 0, max(0, total - 1))
            order = "DESC" if tail else "ASC"
            rows = db.execute(
                "SELECT ordinal,metadata FROM messages WHERE account=? AND conversation=? "
                f"AND ordinal>=? ORDER BY ordinal {order} LIMIT ?",
                (account, remote, start, limit),
            )
            messages, size = [], 0
            for ordinal, metadata in rows:
                # Check serialized size before decoding a potentially large next message.
                if messages and size + len(metadata) > max_chars:
                    break
                value = json.loads(metadata)
                value["_archive_ordinal"] = ordinal
                messages.append(value)
                size += len(metadata)
            if tail:
                messages.reverse()
            first = messages[0]["_archive_ordinal"] if messages else 0
            end = messages[-1]["_archive_ordinal"] + 1 if messages else 0
            return dict(
                header,
                messages=messages,
                history_window={
                    "start": first,
                    "end": end,
                    "total": total,
                },
            )

    def resources(self, account, owner_kind, owner):
        with self._connect() as db:
            rows = db.execute(
                "SELECT metadata FROM resources WHERE account=? AND owner_kind=? AND owner=? ORDER BY ordinal",
                (account, owner_kind, owner),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def file_references(self):
        """Include nested message resources and legacy record metadata."""
        references = set()

        def visit(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "path" and isinstance(item, str):
                        references.add(item)
                    else:
                        visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        with self._connect() as db:
            for (payload,) in db.execute("SELECT payload FROM records"):
                visit(json.loads(payload))
            for (payload,) in db.execute("SELECT metadata FROM messages"):
                visit(json.loads(payload))
            for (payload,) in db.execute("SELECT payload FROM conversation_metadata"):
                visit(json.loads(payload))
            references.update(
                row[0] for row in db.execute("SELECT path FROM resources WHERE path IS NOT NULL")
            )
        return references

    def close(self):
        pass  # Connections are scoped to individual transactions, never held across remote work.

    def uploaded_file_cleanup_pending(self):
        with self._connect() as db:
            row = db.execute(
                "SELECT value FROM maintenance WHERE key='uploaded_file_cleanup_pending'"
            ).fetchone()
        return bool(row and row[0] == "1")

    def complete_uploaded_file_cleanup(self):
        with self._connect() as db:
            db.execute("DELETE FROM maintenance WHERE key='uploaded_file_cleanup_pending'")


class Files:
    def __init__(self, root):
        self.root = Path(root)
        self._lock = threading.RLock()

    def save(self, data):
        with self._lock:
            return self._save(data)

    def save_file(self, source):
        """Persist a local file without reading the complete attachment into memory."""
        with Path(source).open("rb") as stream:
            return self.save_stream(stream)

    def save_stream(self, stream):
        """Hash and publish a binary stream using bounded memory."""
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(prefix=".archive-", dir=self.root)
            digest = hashlib.sha256()
            try:
                with os.fdopen(fd, "wb") as target:
                    while chunk := stream.read(64 * 1024):
                        digest.update(chunk)
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())

                hexdigest = digest.hexdigest()
                path = self.root / hexdigest[:2] / hexdigest
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if path.exists():
                    Path(temporary).unlink()
                else:
                    os.replace(temporary, path)
                    if os.name != "nt" and hasattr(os, "O_DIRECTORY"):
                        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                        try:
                            os.fsync(directory)
                        finally:
                            os.close(directory)
                os.utime(path, None)
                return str(path.relative_to(self.root)), hexdigest
            finally:
                Path(temporary).unlink(missing_ok=True)

    def _save(self, data):
        digest = hashlib.sha256(data).hexdigest()
        path = self.root / digest[:2] / digest
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.exists():
            fd, temporary = tempfile.mkstemp(dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                if os.name != "nt" and hasattr(os, "O_DIRECTORY"):
                    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
            finally:
                Path(temporary).unlink(missing_ok=True)
        os.utime(path, None)  # Protect reused content until its SQL reference commits.
        return str(path.relative_to(self.root)), digest

    def collect(self, references, *, dry_run=True, minimum_age=3600):
        """Only owned hash files; recent saves remain protected before SQL commit."""
        if minimum_age < 3600:
            raise ValueError("archive collection grace must be at least one hour")
        root = self.root.resolve()
        live = {(root / value).resolve() for value in references}
        candidates = []
        with self._lock:
            for path in self.root.glob("??/" + "?" * 64):
                if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
                    continue
                if (
                    not all(c in "0123456789abcdef" for c in path.name)
                    or path.parent.name != path.name[:2]
                ):
                    continue
                if path.resolve() in live or time.time() - path.stat().st_mtime < minimum_age:
                    continue
                candidates.append(str(path.relative_to(self.root)))
                if not dry_run:
                    path.unlink()
        return candidates

    def purge_owned(self):
        """Remove legacy content-addressed upload copies during the v5 migration."""
        with self._lock:
            for path in self.root.glob("??/" + "?" * 64):
                if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
                    continue
                if (
                    not all(char in "0123456789abcdef" for char in path.name)
                    or path.parent.name != path.name[:2]
                ):
                    continue
                path.unlink()

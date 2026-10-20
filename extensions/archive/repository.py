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


class Repository(Protocol):
    def get(self, account, kind, remote): ...
    def put(self, account, kind, remote, value): ...
    def state(self, account, kind, remote, state, error=None): ...
    def conversations(self, account, limit=50, offset=0): ...
    def containers(self, account, limit=50, offset=0): ...
    def messages(self, account, remote, limit=100, offset=0): ...
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
                if versions not in ([(1,)], [(2,)]):
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
            """)
        # Upgrade the existing archive atomically; conversation tables remain intact.
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS downloads("
                "id TEXT PRIMARY KEY, created REAL NOT NULL, payload TEXT NOT NULL)"
            )
            db.execute("UPDATE schema_version SET version=2 WHERE version=1")
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
            row = db.execute(
                "SELECT payload FROM records WHERE account=? AND kind=? AND remote=?",
                (account, kind, remote),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, account, kind, remote, value):
        payload = encode(value)
        digest = hashlib.sha256(payload.encode()).hexdigest()
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
                        encode(item),
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
                    "DELETE FROM messages WHERE account=? AND conversation=?", (account, remote)
                )
                db.executemany(
                    "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)",
                    [
                        (
                            account,
                            remote,
                            i,
                            m.get("id"),
                            m.get("role"),
                            m.get("channel"),
                            m.get("text", ""),
                            encode(m),
                        )
                        for i, m in enumerate(value.get("messages", []))
                    ],
                )
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

    def _write_resources(self, db, account, kind, remote, value):
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
        for i, r in enumerate(resources):
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
                    encode(r),
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
            references.update(
                row[0] for row in db.execute("SELECT path FROM resources WHERE path IS NOT NULL")
            )
        return references

    def close(self):
        pass  # Connections are scoped to individual transactions, never held across remote work.


class Files:
    def __init__(self, root):
        self.root = Path(root)
        self._lock = threading.RLock()

    def save(self, data):
        with self._lock:
            return self._save(data)

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

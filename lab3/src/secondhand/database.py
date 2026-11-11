"""SQLite persistence with explicit transaction boundaries and constraints."""

import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version VALUES(1);
CREATE TABLE IF NOT EXISTS users(
 id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
 password_hash TEXT NOT NULL, nickname TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('user','admin')),
 banned INTEGER NOT NULL DEFAULT 0 CHECK(banned IN (0,1)),
 failed_logins INTEGER NOT NULL DEFAULT 0, locked_until REAL NOT NULL DEFAULT 0,
 last_failure_at REAL NOT NULL DEFAULT 0,
 contact TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions(
 token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
 expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS invites(
 id INTEGER PRIMARY KEY, code_hash TEXT NOT NULL UNIQUE,
 creator_id INTEGER NOT NULL REFERENCES users(id), created_at REAL NOT NULL,
 expires_at REAL NOT NULL, used_by INTEGER REFERENCES users(id),
 revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS categories(
 id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
 enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1))
);
CREATE TABLE IF NOT EXISTS items(
 id INTEGER PRIMARY KEY, seller_id INTEGER NOT NULL REFERENCES users(id),
 category_id INTEGER REFERENCES categories(id), title TEXT NOT NULL DEFAULT '',
 description TEXT NOT NULL DEFAULT '', price_cents INTEGER CHECK(price_cents>0),
 condition TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN
 ('draft','pending_review','rejected','published','reserved','trading','offline','violation','sold')),
 version INTEGER NOT NULL DEFAULT 1, archived INTEGER NOT NULL DEFAULT 0,
 violation INTEGER NOT NULL DEFAULT 0, recovery_requested INTEGER NOT NULL DEFAULT 0,
 reason TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL,
 published_at REAL
);
CREATE TABLE IF NOT EXISTS images(
 id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id),
 content BLOB NOT NULL, mime_type TEXT NOT NULL, digest TEXT NOT NULL,
 sort_no INTEGER NOT NULL, UNIQUE(item_id,sort_no)
);
CREATE TABLE IF NOT EXISTS reviews(
 id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id),
 actor_id INTEGER NOT NULL REFERENCES users(id), reviewed_version INTEGER NOT NULL,
 action TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reservations(
 id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id),
 buyer_id INTEGER NOT NULL REFERENCES users(id), status TEXT NOT NULL CHECK(status IN
 ('pending','confirmed','cancelled','rejected','expired','completed','closed')),
 version INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL, expires_at REAL NOT NULL,
 confirmed_at REAL, completed_at REAL, ended_at REAL, reason TEXT NOT NULL DEFAULT '',
 price_snapshot INTEGER NOT NULL, title_snapshot TEXT NOT NULL,
 category_snapshot INTEGER NOT NULL REFERENCES categories(id), category_name_snapshot TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_reservation ON reservations(item_id)
 WHERE status IN ('pending','confirmed');
CREATE INDEX IF NOT EXISTS reservation_expiry ON reservations(status,expires_at);
CREATE INDEX IF NOT EXISTS item_listing ON items(status,published_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS release_confirmations(
 reservation_id INTEGER NOT NULL REFERENCES reservations(id),
 user_id INTEGER NOT NULL REFERENCES users(id), created_at REAL NOT NULL,
 PRIMARY KEY(reservation_id,user_id)
);
CREATE TABLE IF NOT EXISTS requests(
 actor_id INTEGER NOT NULL REFERENCES users(id), operation TEXT NOT NULL,
 request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, result TEXT NOT NULL,
 created_at REAL NOT NULL, PRIMARY KEY(actor_id,operation,request_id)
);
CREATE TABLE IF NOT EXISTS audit(
 id INTEGER PRIMARY KEY, actor_id INTEGER REFERENCES users(id), role TEXT NOT NULL,
 operation TEXT NOT NULL, object_type TEXT NOT NULL, object_id INTEGER,
 request_id TEXT NOT NULL, result TEXT NOT NULL, reason TEXT NOT NULL,
 before_state TEXT NOT NULL, after_state TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_search ON audit(operation,created_at);
CREATE TABLE IF NOT EXISTS outbox(
 id INTEGER PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
 recipient_id INTEGER NOT NULL REFERENCES users(id),
 reservation_id INTEGER REFERENCES reservations(id),
 body TEXT NOT NULL, created_at REAL NOT NULL, delivered INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages(
 id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL UNIQUE REFERENCES outbox(id),
 recipient_id INTEGER NOT NULL REFERENCES users(id),
 reservation_id INTEGER REFERENCES reservations(id),
 body TEXT NOT NULL, created_at REAL NOT NULL, read_at REAL
);
"""


class Database:
    """A connection per operation supports independent concurrent clients."""

    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)

    def connect(self):
        """Enable foreign keys on every connection, including readers."""
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    @contextmanager
    def transaction(self):
        """Serialize writes; any exception rolls back all affected tables."""
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def read(self):
        """Read multiple related tables from a consistent snapshot."""
        connection = self.connect()
        try:
            connection.execute("BEGIN")
            yield connection
        finally:
            connection.rollback()
            connection.close()

    def check(self):
        """Return storage and cross-aggregate invariant violations."""
        with self.read() as connection:
            errors = [str(tuple(row)) for row in connection.execute("PRAGMA foreign_key_check")]
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                errors.append(integrity)
            rows = connection.execute("""
                SELECT i.id,i.status,
                SUM(CASE WHEN r.status='pending' THEN 1 ELSE 0 END) pending,
                SUM(CASE WHEN r.status='confirmed' THEN 1 ELSE 0 END) confirmed,
                SUM(CASE WHEN r.status='completed' THEN 1 ELSE 0 END) completed
                FROM items i LEFT JOIN reservations r ON r.item_id=i.id GROUP BY i.id
            """)
            for row in rows:
                expected = (int(row["status"] == "reserved"), int(row["status"] == "trading"))
                if (row["pending"], row["confirmed"]) != expected:
                    errors.append(f"item {row['id']}: active reservation mismatch")
                if row["status"] == "sold" and row["completed"] != 1:
                    errors.append(f"item {row['id']}: completed reservation mismatch")
        return errors

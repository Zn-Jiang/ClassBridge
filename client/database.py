"""Lightweight local SQLite database for the desktop client.

Stores cached messages so the client can display the last-known state
even when the server is unreachable.  The database lives in the per-user
appdata directory (``%APPDATA%/ClassBridge/client.db`` on Windows).
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from shared.paths import CLIENT_DATABASE_PATH, ensure_client_appdata_dir

logger = logging.getLogger("kg.client.database")

SCHEMA_VERSION = 1

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS cached_messages (
    db_id          INTEGER PRIMARY KEY,
    sender_id      TEXT    NOT NULL,
    sender_name    TEXT    NOT NULL,
    content        TEXT    NOT NULL,
    msg_type       TEXT    NOT NULL,
    status         TEXT    NOT NULL DEFAULT 'unread',
    resend_count   INTEGER NOT NULL DEFAULT 0,
    timestamp      TEXT    NOT NULL,
    resend_time    TEXT,
    group_id       TEXT,
    source_message_id INTEGER,
    cached_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS client_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class ClientDatabase:
    """Thread-safe local cache database for the desktop client."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = db_path or CLIENT_DATABASE_PATH
        ensure_client_appdata_dir()
        self._conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        if self._conn is not None:
            return
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_CREATE_TABLES)
        self._migrate()
        self._conn.commit()
        logger.info("Client database opened: %s", self._db_path)

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.close()
        except Exception:
            pass
        self._conn = None

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    # ------------------------------------------------------------------
    # message cache
    # ------------------------------------------------------------------

    def cache_messages(self, messages: List[Dict[str, Any]]) -> None:
        """Replace the entire message cache with *messages*."""
        if not self._conn:
            return
        now = _utcnow()
        with self._tx() as cur:
            cur.execute("DELETE FROM cached_messages")
            for msg in messages:
                cur.execute(
                    """INSERT OR REPLACE INTO cached_messages
                       (db_id, sender_id, sender_name, content, msg_type, status,
                        resend_count, timestamp, resend_time, group_id,
                        source_message_id, cached_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        msg["db_id"],
                        msg.get("sender_id", ""),
                        msg.get("sender_name", ""),
                        msg.get("content", ""),
                        msg.get("msg_type", "normal"),
                        msg.get("status", "unread"),
                        msg.get("resend_count", 0),
                        msg.get("timestamp", ""),
                        msg.get("resend_time"),
                        msg.get("group_id"),
                        msg.get("source_message_id"),
                        now,
                    ),
                )

    def get_cached_messages(self) -> List[Dict[str, Any]]:
        """Return all cached messages, newest first."""
        if not self._conn:
            return []
        cur = self._conn.execute(
            """SELECT * FROM cached_messages
               ORDER BY COALESCE(resend_time, timestamp) DESC, db_id DESC"""
        )
        return [dict(row) for row in cur.fetchall()]

    def upsert_message(self, message: Dict[str, Any]) -> None:
        """Insert or update a single message in the cache."""
        if not self._conn:
            return
        now = _utcnow()
        with self._tx() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO cached_messages
                   (db_id, sender_id, sender_name, content, msg_type, status,
                    resend_count, timestamp, resend_time, group_id,
                    source_message_id, cached_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    message["db_id"],
                    message.get("sender_id", ""),
                    message.get("sender_name", ""),
                    message.get("content", ""),
                    message.get("msg_type", "normal"),
                    message.get("status", "unread"),
                    message.get("resend_count", 0),
                    message.get("timestamp", ""),
                    message.get("resend_time"),
                    message.get("group_id"),
                    message.get("source_message_id"),
                    now,
                ),
            )

    def update_message_status(self, db_id: int, status: str) -> None:
        """Update the cached status of a single message."""
        if not self._conn:
            return
        with self._tx() as cur:
            cur.execute(
                "UPDATE cached_messages SET status = ? WHERE db_id = ?",
                (status, db_id),
            )

    def remove_message(self, db_id: int) -> None:
        """Remove a message from the cache."""
        if not self._conn:
            return
        with self._tx() as cur:
            cur.execute("DELETE FROM cached_messages WHERE db_id = ?", (db_id,))

    # ------------------------------------------------------------------
    # key-value meta
    # ------------------------------------------------------------------

    def get_meta(self, key: str, default: str = "") -> str:
        """Read a metadata value."""
        if not self._conn:
            return default
        cur = self._conn.execute(
            "SELECT value FROM client_meta WHERE key = ?", (key,)
        )
        row = cur.fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        """Write a metadata value."""
        if not self._conn:
            return
        with self._tx() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO client_meta (key, value) VALUES (?, ?)",
                (key, value),
            )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @contextmanager
    def _tx(self):
        """Yield a cursor inside a transaction that commits on success."""
        assert self._conn is not None
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    def _migrate(self) -> None:
        """Handle future schema migrations."""
        version = int(self.get_meta("schema_version", "0"))
        if version < 1:
            self.set_meta("schema_version", str(SCHEMA_VERSION))


# ------------------------------------------------------------------
# module-level helpers
# ------------------------------------------------------------------

def _utcnow() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def message_to_cache_dict(msg) -> Dict[str, Any]:
    """Convert a ClientMessage (from models.py) to a cache-ready dict."""
    return {
        "db_id": msg.db_id,
        "sender_id": msg.sender_id,
        "sender_name": msg.sender_name,
        "content": msg.content,
        "msg_type": msg.msg_type.value if hasattr(msg.msg_type, "value") else msg.msg_type,
        "status": msg.status.value if hasattr(msg.status, "value") else msg.status,
        "resend_count": msg.resend_count,
        "timestamp": msg.timestamp,
        "resend_time": msg.resend_time,
        "group_id": msg.group_id,
        "source_message_id": msg.source_message_id,
    }

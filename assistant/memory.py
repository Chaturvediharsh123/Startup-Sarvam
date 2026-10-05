"""SQLite conversation memory: turns, actions and long-term facts.

One :class:`Memory` object is shared by the UI thread, the brain worker and the
pipeline, so every query runs under a lock on a single connection opened with
``check_same_thread=False``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
ACTION_STATUSES = ("ok", "error", "denied", "blocked")

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    language TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    language TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    args TEXT NOT NULL,
    result TEXT,
    status TEXT NOT NULL CHECK (status IN ('ok', 'error', 'denied', 'blocked')),
    duration_ms INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actions_session ON actions(session_id, id);
CREATE TABLE IF NOT EXISTS facts (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# version -> function that upgrades the schema from (version - 1) to version.
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: lambda conn: conn.executescript(_SCHEMA_V1),
}


def _now() -> str:
    """Current local time as ISO-8601 text."""
    return datetime.now().isoformat(timespec="seconds")


def _normalise_key(key: str) -> str:
    """Fact keys are case-insensitive and whitespace-trimmed."""
    return " ".join(key.strip().lower().split())


def migrate(conn: sqlite3.Connection, target: int = SCHEMA_VERSION) -> int:
    """Bring the database schema up to ``target`` without losing data.

    Returns:
        The schema version after migration.
    """
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] or 0
    for version in range(current + 1, target + 1):
        logger.info("Migrating memory database to schema version %d", version)
        MIGRATIONS[version](conn)
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
    conn.commit()
    return max(current, target)


class Memory:
    """Thread-safe conversation memory backed by SQLite.

    Args:
        db_path: database file, or ``":memory:"`` for tests.
        language: language code recorded on new sessions.
    """

    def __init__(self, db_path: Path | str, language: str | None = None) -> None:
        path = str(db_path)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._closed = False
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            migrate(self._conn)
        self.session_id = self._start_session(language)

    # -- context manager -------------------------------------------------
    def __enter__(self) -> Memory:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- internals -------------------------------------------------------
    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        """Run one write statement under the lock and commit."""
        with self._lock:
            if self._closed:
                raise RuntimeError("Memory is closed")
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        """Run one read query under the lock."""
        with self._lock:
            if self._closed:
                raise RuntimeError("Memory is closed")
            return self._conn.execute(sql, params).fetchall()

    def _start_session(self, language: str | None) -> int:
        cursor = self._execute(
            "INSERT INTO sessions (started_at, language) VALUES (?, ?)", (_now(), language)
        )
        return int(cursor.lastrowid or 0)

    # -- messages --------------------------------------------------------
    def add_message(self, role: str, content: str, language: str | None = None) -> int:
        """Store one conversation turn. ``role`` is ``user`` or ``assistant``."""
        if role not in ("user", "assistant"):
            raise ValueError(f"role must be 'user' or 'assistant', got {role!r}")
        cursor = self._execute(
            "INSERT INTO messages (session_id, role, content, language, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (self.session_id, role, content, language, _now()),
        )
        return int(cursor.lastrowid or 0)

    def recent_messages(self, limit: int = 10, all_sessions: bool = False) -> list[dict[str, str]]:
        """Return the last ``limit`` turns, oldest first, as LLM-ready dicts."""
        if limit <= 0:
            return []
        where = "" if all_sessions else "WHERE session_id = ?"
        params: tuple[Any, ...] = (limit,) if all_sessions else (self.session_id, limit)
        rows = self._query(
            f"SELECT role, content FROM messages {where} ORDER BY id DESC LIMIT ?",  # noqa: S608
            params,
        )
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def search_messages(self, text: str, limit: int = 20) -> list[dict[str, Any]]:
        """Find turns in any session whose content contains ``text`` (newest first)."""
        pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = self._query(
            "SELECT id, session_id, role, content, language, created_at FROM messages "
            "WHERE content LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT ?",
            (pattern, limit),
        )
        return [dict(r) for r in rows]

    # -- actions ---------------------------------------------------------
    def log_action(
        self,
        name: str,
        args: dict[str, Any],
        result: str,
        status: str,
        duration_ms: int = 0,
    ) -> int:
        """Record one action attempt with its outcome."""
        if status not in ACTION_STATUSES:
            raise ValueError(f"status must be one of {ACTION_STATUSES}, got {status!r}")
        cursor = self._execute(
            "INSERT INTO actions (session_id, name, args, result, status, duration_ms, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self.session_id,
                name,
                json.dumps(args, ensure_ascii=False),
                result,
                status,
                int(duration_ms),
                _now(),
            ),
        )
        return int(cursor.lastrowid or 0)

    def last_action(self) -> dict[str, Any] | None:
        """The most recent *successful* action in this session, or ``None``.

        This is what "isko" / "it" refers to in a follow-up command.
        """
        rows = self._query(
            "SELECT name, args, result, created_at FROM actions "
            "WHERE session_id = ? AND status = 'ok' ORDER BY id DESC LIMIT 1",
            (self.session_id,),
        )
        if not rows:
            return None
        row = dict(rows[0])
        row["args"] = json.loads(row["args"])
        return row

    # -- facts -----------------------------------------------------------
    def remember(self, key: str, value: str) -> None:
        """Store or replace a long-term fact such as a name or city."""
        norm = _normalise_key(key)
        if not norm:
            raise ValueError("fact key must not be empty")
        self._execute(
            "INSERT INTO facts (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (norm, value.strip(), _now()),
        )

    def recall(self, key: str) -> str | None:
        """Return a stored fact, or ``None``."""
        rows = self._query("SELECT value FROM facts WHERE key = ?", (_normalise_key(key),))
        return rows[0]["value"] if rows else None

    def all_facts(self) -> dict[str, str]:
        """Every stored fact, sorted by key."""
        rows = self._query("SELECT key, value FROM facts ORDER BY key")
        return {r["key"]: r["value"] for r in rows}

    def forget(self, key: str) -> bool:
        """Delete a fact. Returns True if it existed."""
        cursor = self._execute("DELETE FROM facts WHERE key = ?", (_normalise_key(key),))
        return cursor.rowcount > 0

    # -- summaries and lifecycle ----------------------------------------
    def context_note(self) -> str:
        """Short text for the LLM: known facts plus the last successful action."""
        parts: list[str] = []
        facts = self.all_facts()
        if facts:
            known = "; ".join(f"{k}: {v}" for k, v in facts.items())
            parts.append(f"Known facts about the user: {known}")
        last = self.last_action()
        if last:
            args = json.dumps(last["args"], ensure_ascii=False)
            parts.append(f"Last action: {last['name']}({args}) -> {last['result']}")
        return "\n".join(parts)

    def clear_session(self) -> None:
        """Forget this session's turns and actions ("sab bhool jao"). Facts stay."""
        self._execute("DELETE FROM messages WHERE session_id = ?", (self.session_id,))
        self._execute("DELETE FROM actions WHERE session_id = ?", (self.session_id,))

    def end_session(self) -> None:
        """Mark the current session as finished."""
        self._execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (_now(), self.session_id))

    def close(self) -> None:
        """End the session and close the database. Safe to call twice."""
        with self._lock:
            if self._closed:
                return
            try:
                self.end_session()
            except sqlite3.Error:
                logger.exception("Could not mark session as ended")
            self._closed = True
            self._conn.close()

"""SQLite persistence: the source of truth for memories, workflows and verification history.

FTS5 tables hold searchable text. The tokenizer keeps Unicode combining marks (``M*``) inside
tokens; SQLite's default treats Devanagari vowel signs as separators, which splits "बिजली"
into fragments and makes "बिल" match it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from memory.models import Memory, MemoryNotFoundError, MemoryVerification, as_utc
from memory.workflow import Workflow, WorkflowVerification

SCHEMA_VERSION = 1
FTS_TOKENIZER = "unicode61 remove_diacritics 0 categories 'L* N* Co M*'"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_verified_at TEXT,
    expires_at TEXT,
    metadata TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_type_status ON memories(type, status);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(id UNINDEXED, body, tokenize="{FTS_TOKENIZER}");

CREATE TABLE IF NOT EXISTS memory_verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    verified_at TEXT NOT NULL,
    method TEXT NOT NULL,
    evidence TEXT NOT NULL,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_memory_verifications ON memory_verifications(memory_id);

CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    confidence REAL NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_verified_at TEXT,
    expires_at TEXT,
    source_execution_id TEXT,
    data TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS workflows_fts USING fts5(id UNINDEXED, body, tokenize="{FTS_TOKENIZER}");

CREATE TABLE IF NOT EXISTS workflow_versions (
    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    data TEXT NOT NULL,
    archived_at TEXT NOT NULL,
    PRIMARY KEY (workflow_id, version)
);

CREATE TABLE IF NOT EXISTS workflow_verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    workflow_version INTEGER,
    execution_id TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    success INTEGER NOT NULL,
    evidence TEXT NOT NULL,
    observed_changes TEXT NOT NULL,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_workflow_verifications ON workflow_verifications(workflow_id);
"""


def _iso(value: datetime | None) -> str | None:
    value = as_utc(value)
    return None if value is None else value.isoformat(timespec="microseconds")


def _dt(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def tokenize(text: str) -> list[str]:
    """Lower-cased word tokens, keeping combining marks (Devanagari matras) inside words."""
    tokens: list[str] = []
    current: list[str] = []
    for ch in text.casefold():
        if ch.isalnum() or unicodedata.category(ch).startswith("M"):
            current.append(ch)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tokens


class SQLiteStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if str(path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        with self.transaction() as c:
            # Statement by statement: executescript() would commit outside our transaction.
            for statement in filter(str.strip, _SCHEMA.split(";")):
                c.execute(statement)
            c.execute("INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    # ------------------------------------------------------------------ memories

    @staticmethod
    def _memory_row(m: Memory) -> tuple[Any, ...]:
        return (
            m.id, m.type.value, json.dumps(m.content, ensure_ascii=False), m.source.value, m.confidence,
            m.status.value, _iso(m.created_at), _iso(m.updated_at), _iso(m.last_verified_at), _iso(m.expires_at),
            m.metadata.model_dump_json(),
        )

    @staticmethod
    def _memory_from_row(row: sqlite3.Row) -> Memory:
        return Memory(
            id=row["id"], type=row["type"], content=json.loads(row["content"]), source=row["source"],
            confidence=row["confidence"], status=row["status"], created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]), last_verified_at=_dt(row["last_verified_at"]),
            expires_at=_dt(row["expires_at"]), metadata=json.loads(row["metadata"]),
        )

    def insert_memory(self, m: Memory) -> None:
        with self.transaction() as c:
            try:
                c.execute("INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?)", self._memory_row(m))
            except sqlite3.IntegrityError:
                raise ValueError(f"memory {m.id} already exists") from None
            c.execute("INSERT INTO memories_fts(id, body) VALUES (?, ?)", (m.id, m.text()))

    def replace_memory(self, m: Memory) -> None:
        with self.transaction() as c:
            cur = c.execute(
                "UPDATE memories SET type=?, content=?, source=?, confidence=?, status=?, created_at=?, updated_at=?,"
                " last_verified_at=?, expires_at=?, metadata=? WHERE id=?",
                (*self._memory_row(m)[1:], m.id),
            )
            if cur.rowcount == 0:
                raise MemoryNotFoundError(m.id)
            c.execute("DELETE FROM memories_fts WHERE id = ?", (m.id,))
            c.execute("INSERT INTO memories_fts(id, body) VALUES (?, ?)", (m.id, m.text()))

    def get_memory(self, memory_id: str) -> Memory | None:
        rows = self._query("SELECT * FROM memories WHERE id = ?", (memory_id,))
        return self._memory_from_row(rows[0]) if rows else None

    def get_memories(self, ids: Iterable[str]) -> dict[str, Memory]:
        ids = list(dict.fromkeys(ids))
        if not ids:
            return {}
        rows = self._query(f"SELECT * FROM memories WHERE id IN ({','.join('?' * len(ids))})", ids)
        return {r["id"]: self._memory_from_row(r) for r in rows}

    def list_memories(self, types: Iterable[str] | None = None, limit: int = 1000) -> list[Memory]:
        sql, params = "SELECT * FROM memories", []
        types = list(types or [])
        if types:
            sql += f" WHERE type IN ({','.join('?' * len(types))})"
            params += types
        rows = self._query(sql + " ORDER BY updated_at DESC LIMIT ?", [*params, limit])
        return [self._memory_from_row(r) for r in rows]

    def delete_memory(self, memory_id: str) -> bool:
        with self.transaction() as c:
            c.execute("DELETE FROM memories_fts WHERE id = ?", (memory_id,))
            return c.execute("DELETE FROM memories WHERE id = ?", (memory_id,)).rowcount > 0

    def add_memory_verification(self, v: MemoryVerification) -> None:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO memory_verifications(memory_id, verified_at, method, evidence, notes) VALUES (?,?,?,?,?)",
                (v.memory_id, _iso(v.verified_at), v.method, json.dumps(v.evidence, ensure_ascii=False), v.notes),
            )

    def memory_verifications(self, memory_id: str) -> list[MemoryVerification]:
        rows = self._query("SELECT * FROM memory_verifications WHERE memory_id = ? ORDER BY verified_at", (memory_id,))
        return [MemoryVerification(memory_id=r["memory_id"], verified_at=_dt(r["verified_at"]), method=r["method"],
                                   evidence=json.loads(r["evidence"]), notes=r["notes"]) for r in rows]

    # ------------------------------------------------------------------ keyword search

    def keyword_search(self, kind: str, terms: list[str], limit: int) -> list[tuple[str, str]]:
        """Candidate ids (+ indexed text) whose body contains any term (prefix match).
        ``kind`` is 'memories' or 'workflows'."""
        if kind not in ("memories", "workflows") or not terms:
            return []
        match = " OR ".join('"' + t.replace('"', '""') + '"*' for t in terms)
        rows = self._query(f"SELECT id, body FROM {kind}_fts WHERE {kind}_fts MATCH ? ORDER BY rank LIMIT ?", (match, limit))
        return [(r["id"], r["body"]) for r in rows]

    # ------------------------------------------------------------------ workflows

    @staticmethod
    def _workflow_data(w: Workflow) -> str:
        return w.model_dump_json(exclude={"verification_history"})

    def insert_workflow(self, w: Workflow) -> None:
        with self.transaction() as c:
            try:
                c.execute(
                    "INSERT INTO workflows VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (w.id, w.goal, w.version, w.status.value, w.confidence, _iso(w.created_at), _iso(w.updated_at),
                     _iso(w.last_verified_at), _iso(w.expires_at), w.source_execution_id, self._workflow_data(w)),
                )
            except sqlite3.IntegrityError:
                raise ValueError(f"workflow {w.id} already exists") from None
            c.execute("INSERT INTO workflows_fts(id, body) VALUES (?, ?)", (w.id, w.text()))
            for v in w.verification_history:
                self._insert_workflow_verification(c, w.id, v)

    def replace_workflow(self, w: Workflow, *, archive: Workflow | None = None) -> None:
        """Overwrite the workflow row (verification rows are append-only and untouched).
        ``archive`` stores the previous procedural version before overwriting."""
        with self.transaction() as c:
            if archive is not None:
                c.execute("INSERT OR REPLACE INTO workflow_versions VALUES (?,?,?,?)",
                          (archive.id, archive.version, self._workflow_data(archive), _iso(w.updated_at)))
            cur = c.execute(
                "UPDATE workflows SET goal=?, version=?, status=?, confidence=?, created_at=?, updated_at=?,"
                " last_verified_at=?, expires_at=?, source_execution_id=?, data=? WHERE id=?",
                (w.goal, w.version, w.status.value, w.confidence, _iso(w.created_at), _iso(w.updated_at),
                 _iso(w.last_verified_at), _iso(w.expires_at), w.source_execution_id, self._workflow_data(w), w.id),
            )
            if cur.rowcount == 0:
                raise MemoryNotFoundError(w.id)
            c.execute("DELETE FROM workflows_fts WHERE id = ?", (w.id,))
            c.execute("INSERT INTO workflows_fts(id, body) VALUES (?, ?)", (w.id, w.text()))

    @staticmethod
    def _insert_workflow_verification(c: sqlite3.Connection, workflow_id: str, v: WorkflowVerification) -> None:
        c.execute(
            "INSERT INTO workflow_verifications(workflow_id, workflow_version, execution_id, verified_at, success,"
            " evidence, observed_changes, notes) VALUES (?,?,?,?,?,?,?,?)",
            (workflow_id, v.workflow_version, v.execution_id, _iso(v.timestamp), int(v.success),
             json.dumps(v.evidence, ensure_ascii=False), json.dumps(v.observed_changes, ensure_ascii=False), v.notes),
        )

    def add_workflow_verification(self, workflow_id: str, v: WorkflowVerification) -> None:
        with self.transaction() as c:
            self._insert_workflow_verification(c, workflow_id, v)

    def _verifications(self, workflow_ids: list[str]) -> dict[str, list[WorkflowVerification]]:
        out: dict[str, list[WorkflowVerification]] = {i: [] for i in workflow_ids}
        if not workflow_ids:
            return out
        rows = self._query(
            f"SELECT * FROM workflow_verifications WHERE workflow_id IN ({','.join('?' * len(workflow_ids))})"
            " ORDER BY verified_at, id", workflow_ids,
        )
        for r in rows:
            out[r["workflow_id"]].append(WorkflowVerification(
                execution_id=r["execution_id"], timestamp=_dt(r["verified_at"]), success=bool(r["success"]),
                evidence=json.loads(r["evidence"]), observed_changes=json.loads(r["observed_changes"]),
                notes=r["notes"], workflow_version=r["workflow_version"],
            ))
        return out

    def get_workflows(self, ids: Iterable[str]) -> dict[str, Workflow]:
        ids = list(dict.fromkeys(ids))
        if not ids:
            return {}
        rows = self._query(f"SELECT id, data FROM workflows WHERE id IN ({','.join('?' * len(ids))})", ids)
        history = self._verifications([r["id"] for r in rows])
        return {r["id"]: Workflow.model_validate({**json.loads(r["data"]), "verification_history": history[r["id"]]})
                for r in rows}

    def get_workflow(self, workflow_id: str) -> Workflow | None:
        return self.get_workflows([workflow_id]).get(workflow_id)

    def list_workflows(self, limit: int = 1000) -> list[Workflow]:
        ids = [r["id"] for r in self._query("SELECT id FROM workflows ORDER BY updated_at DESC LIMIT ?", (limit,))]
        found = self.get_workflows(ids)
        return [found[i] for i in ids if i in found]

    def workflow_versions(self, workflow_id: str) -> list[Workflow]:
        rows = self._query("SELECT data FROM workflow_versions WHERE workflow_id = ? ORDER BY version", (workflow_id,))
        return [Workflow.model_validate_json(r["data"]) for r in rows]

    def delete_workflow(self, workflow_id: str) -> bool:
        with self.transaction() as c:
            c.execute("DELETE FROM workflows_fts WHERE id = ?", (workflow_id,))
            return c.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,)).rowcount > 0

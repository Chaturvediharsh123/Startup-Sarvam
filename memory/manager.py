"""MemoryManager: the only entry point other components use for memory.

Nothing outside the Memory Block touches SQLite or LanceDB directly. Retrieval never counts
as verification; only ``verify`` / ``record_workflow_verification`` move verification
timestamps.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from memory.config import MemorySettings
from memory.models import (
    Memory,
    MemoryMetadata,
    MemoryNotFoundError,
    MemorySource,
    MemoryStatus,
    MemoryType,
    MemoryUpdate,
    MemoryVerification,
    RetrievedMemory,
    WorkflowNotVerifiedError,
    as_utc,
    utcnow,
)
from memory.privacy import SensitiveDataFilter, marker
from memory.retrieval import HybridRetriever, MemoryContext
from memory.semantic_store import Embedder, FastEmbedEmbedder, SemanticStore
from memory.sqlite_store import SQLiteStore
from memory.workflow import (
    PROCEDURAL_FIELDS,
    Workflow,
    WorkflowMatch,
    WorkflowStatus,
    WorkflowUpdate,
    WorkflowVerification,
)
from memory.working import WorkingMemory

logger = logging.getLogger("memory.manager")


class MemoryManager:
    def __init__(
        self,
        settings: MemorySettings | None = None,
        *,
        embedder: Embedder | None = None,
        enable_semantic: bool | None = None,
    ) -> None:
        self.settings = settings or MemorySettings()
        self._store = SQLiteStore(self.settings.db_path)
        self._semantic: SemanticStore | None = None
        if self.settings.semantic_enabled if enable_semantic is None else enable_semantic:
            try:
                self._semantic = SemanticStore(
                    self.settings.lancedb_path,
                    embedder or FastEmbedEmbedder(self.settings.embedding_model, self.settings.embedding_cache_dir),
                )
            except ImportError:
                logger.warning("lancedb/fastembed not installed; falling back to keyword (FTS5) retrieval only")
        self._retriever = HybridRetriever(self._store, self._semantic, self.settings)
        self._privacy = SensitiveDataFilter()
        self._log_lock = threading.Lock()
        self.working = WorkingMemory(self.settings.working_max_items)

    def __enter__(self) -> MemoryManager:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._store.close()

    @property
    def semantic_enabled(self) -> bool:
        return self._semantic is not None

    # ================================================================== memories

    def save(self, memory: Memory | Mapping[str, Any]) -> Memory:
        """Persist a new memory after sensitive-data filtering.

        Memories stated by the user count as verified at creation ("user_statement");
        inferred or observed memories start unverified."""
        m = memory.model_copy(deep=True) if isinstance(memory, Memory) else Memory.model_validate(memory)
        m = self._redact_memory(m)
        verification = None
        if m.source is MemorySource.USER and m.last_verified_at is None:
            m.last_verified_at = m.created_at
            m.metadata.verification_method = m.metadata.verification_method or "user_statement"
            verification = MemoryVerification(memory_id=m.id, verified_at=m.created_at, method="user_statement")
        self._store.insert_memory(m)
        if verification is not None:
            self._store.add_memory_verification(verification)
        self._index("memories", m.id, m.text())
        logger.info("memory.saved id=%s type=%s source=%s redactions=%s", m.id, m.type.value, m.source.value,
                    m.metadata.redactions)
        return m

    def get(self, memory_id: str) -> Memory | None:
        return self._store.get_memory(memory_id)

    def list_memories(self, types: Iterable[MemoryType | str] | None = None, limit: int = 100) -> list[Memory]:
        return self._store.list_memories([MemoryType(t).value for t in types or []], limit)

    def update(self, memory_id: str, data: MemoryUpdate | Mapping[str, Any]) -> Memory:
        """Apply a partial update. Changing ``content`` clears ``last_verified_at``, since the
        old verification applied to the old content."""
        current = self._require_memory(memory_id)
        changes = (data if isinstance(data, MemoryUpdate) else MemoryUpdate.model_validate(data)).model_dump(exclude_unset=True)
        merged = current.model_dump()
        if "metadata" in changes:
            merged["metadata"] = {**merged["metadata"], **(changes.pop("metadata") or {})}
        if "content" in changes and changes["content"] != current.content:
            merged["last_verified_at"] = None
        merged.update(changes)
        merged["updated_at"] = utcnow()
        updated = self._redact_memory(Memory.model_validate(merged))
        self._store.replace_memory(updated)
        if updated.text() != current.text():
            self._index("memories", updated.id, updated.text())
        return updated

    def verify(
        self,
        memory_id: str,
        *,
        method: str,
        evidence: list[dict[str, Any]] | None = None,
        notes: str | None = None,
        verified_at: datetime | None = None,
    ) -> Memory:
        """Record that the memory was confirmed (user confirmation, successful execution,
        current observation, ...). This is the only way ``last_verified_at`` moves forward."""
        current = self._require_memory(memory_id)
        when = as_utc(verified_at) or utcnow()
        evidence = self._privacy.redact(evidence or []).value
        meta = current.metadata.model_copy(update={"verification_method": method})
        updated = current.model_copy(update={"last_verified_at": when, "updated_at": utcnow(), "metadata": meta})
        self._store.replace_memory(updated)
        self._store.add_memory_verification(MemoryVerification(memory_id=memory_id, verified_at=when, method=method,
                                                               evidence=evidence, notes=notes))
        return updated

    def verification_history(self, memory_id: str) -> list[MemoryVerification]:
        return self._store.memory_verifications(memory_id)

    def invalidate(self, memory_id: str, reason: str | None = None) -> Memory:
        """Mark as invalid (kept for audit, excluded from search). Use ``forget`` to delete."""
        current = self._require_memory(memory_id)
        meta = current.metadata.model_copy(update={"invalidated_reason": reason})
        updated = current.model_copy(update={"status": MemoryStatus.INVALID, "updated_at": utcnow(), "metadata": meta})
        self._store.replace_memory(updated)
        return updated

    def forget(self, memory_id: str) -> bool:
        """Permanently delete a memory, its verification records and its vector."""
        deleted = self._store.delete_memory(memory_id)
        self._unindex("memories", memory_id)
        if deleted:
            logger.info("memory.forgotten id=%s", memory_id)
        return deleted

    def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        types: Iterable[MemoryType | str] | None = None,
        tags: Iterable[str] | None = None,
        include_expired: bool = True,
        include_invalid: bool = False,
        now: datetime | None = None,
    ) -> list[RetrievedMemory]:
        """Hybrid (keyword + semantic) search. Results are context: check ``freshness`` /
        ``caveat`` before presenting anything as current fact."""
        hits = self._retriever.search_memories(query, limit=limit, types=types, tags=tags,
                                               include_expired=include_expired, include_invalid=include_invalid, now=now)
        if self.settings.retrieval_log_path is not None:
            search_id = f"srch_{uuid.uuid4().hex[:12]}"
            hits = [h.model_copy(update={"search_id": search_id}) for h in hits]
            self._log_retrieval({
                "event": "search", "search_id": search_id, "query": self._privacy.redact_text(query).value,
                "results": [{"id": h.memory.id, "score": h.score, "keyword": h.keyword_score,
                             "semantic": h.semantic_score, "freshness": h.freshness.value} for h in hits],
            })
        return hits

    def record_search_feedback(self, search_id: str, used_memory_ids: list[str], *, note: str | None = None) -> None:
        """Tell the retrieval log which results the planner actually used (``[]`` = none were
        relevant). These pairs become evaluation cases for tuning (memory.evaluation)."""
        if self.settings.retrieval_log_path is not None:
            self._log_retrieval({"event": "feedback", "search_id": search_id, "used": list(used_memory_ids), "note": note})

    def _log_retrieval(self, entry: dict[str, Any]) -> None:
        path = Path(self.settings.retrieval_log_path)  # type: ignore[arg-type]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_lock, path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"at": utcnow().isoformat(), **entry}, ensure_ascii=False) + "\n")
        except OSError:
            logger.warning("could not write retrieval log %s", path, exc_info=True)

    def build_context(self, query: str, *, limit: int | None = None, workflow_limit: int = 2,
                      include_working: bool = True) -> MemoryContext:
        return MemoryContext(
            query=query,
            memories=self.search(query, limit=limit),
            workflows=self.find_workflows(query, limit=workflow_limit) if workflow_limit else [],
            working=self.working.snapshot() if include_working else None,
        )

    # ================================================================== workflows

    def save_workflow(self, workflow: Workflow | Mapping[str, Any]) -> Workflow:
        """Store a workflow extracted from a *verified successful* execution.

        Raises WorkflowNotVerifiedError if ``verification_history`` has no success: failed
        attempts belong in episodes, not in reusable workflows."""
        w = workflow.model_copy(deep=True) if isinstance(workflow, Workflow) else Workflow.model_validate(workflow)
        successes = [v for v in w.verification_history if v.success]
        if not successes:
            raise WorkflowNotVerifiedError(
                "workflows are stored only after a verified successful execution; store failed attempts as episodes"
            )
        for v in w.verification_history:
            if v.workflow_version is None:
                v.workflow_version = w.version
        latest_ok = max(v.timestamp for v in successes)
        w.last_verified_at = max(filter(None, [w.last_verified_at, latest_ok]))
        w.source_execution_id = w.source_execution_id or successes[-1].execution_id
        w = self._redact_workflow(w)
        self._store.insert_workflow(w)
        self._index("workflows", w.id, w.text())
        logger.info("workflow.saved id=%s version=%d steps=%d", w.id, w.version, len(w.steps))
        return w

    def get_workflow(self, task: str, *, current_only: bool = False) -> WorkflowMatch | None:
        """Best workflow for a task, or None. Even a ``current`` match is only a starting
        point: re-check its preconditions against the live environment before replaying."""
        for match in self.find_workflows(task, limit=5):
            if not current_only or match.is_current:
                return match
        return None

    def find_workflows(self, task: str, *, limit: int = 3, include_invalid: bool = False,
                       now: datetime | None = None) -> list[WorkflowMatch]:
        return self._retriever.search_workflows(task, limit=limit, include_invalid=include_invalid, now=now)

    def get_workflow_by_id(self, workflow_id: str) -> Workflow | None:
        return self._store.get_workflow(workflow_id)

    def list_workflows(self, limit: int = 100) -> list[Workflow]:
        return self._store.list_workflows(limit)

    def workflow_versions(self, workflow_id: str) -> list[Workflow]:
        """Archived earlier procedural versions (oldest first)."""
        return self._store.workflow_versions(workflow_id)

    def update_workflow(
        self,
        workflow_id: str,
        data: WorkflowUpdate | Mapping[str, Any],
        *,
        verification: WorkflowVerification | None = None,
    ) -> Workflow:
        """Partial update. Changing preconditions/steps/outcomes/failure conditions bumps the
        version, archives the previous one and resets ``last_verified_at`` until the new
        version succeeds (pass ``verification`` to record that run in the same call)."""
        current = self._require_workflow(workflow_id)
        changes = (data if isinstance(data, WorkflowUpdate) else WorkflowUpdate.model_validate(data)).model_dump(exclude_unset=True)
        base = current.model_dump(exclude={"verification_history"})
        if "metadata" in changes:
            changes["metadata"] = {**base["metadata"], **(changes["metadata"] or {})}
        candidate = Workflow.model_validate({**base, **changes, "verification_history": current.verification_history})
        procedural = candidate.model_dump(include=set(PROCEDURAL_FIELDS)) != current.model_dump(include=set(PROCEDURAL_FIELDS))
        archive = None
        update: dict[str, Any] = {"updated_at": utcnow()}
        if procedural:
            archive = current
            update |= {"version": current.version + 1, "last_verified_at": None}
        updated = self._redact_workflow(candidate.model_copy(update=update))
        self._store.replace_workflow(updated, archive=archive)
        self._index("workflows", updated.id, updated.text())
        if procedural:
            logger.info("workflow.versioned id=%s version=%d", updated.id, updated.version)
        if verification is not None:
            return self.record_workflow_verification(workflow_id, verification)
        return updated

    def record_workflow_verification(self, workflow_id: str, verification: WorkflowVerification | Mapping[str, Any]) -> Workflow:
        """Append a run to the history. Success refreshes ``last_verified_at`` and nudges
        confidence up; failure lowers confidence (the workflow then reads as ``failing``)."""
        current = self._require_workflow(workflow_id)
        v = verification.model_copy(deep=True) if isinstance(verification, WorkflowVerification) \
            else WorkflowVerification.model_validate(verification)
        if v.workflow_version is None:
            v.workflow_version = current.version
        redacted = self._privacy.redact(v.model_dump(mode="json"))
        v = WorkflowVerification.model_validate(redacted.value)
        self._store.add_workflow_verification(workflow_id, v)
        update: dict[str, Any] = {"updated_at": utcnow()}
        if v.success:
            update["last_verified_at"] = max(filter(None, [current.last_verified_at, v.timestamp]))
            update["confidence"] = round(min(0.95, current.confidence + 0.05), 4)
        else:
            update["confidence"] = round(max(0.05, current.confidence - 0.15), 4)
        self._store.replace_workflow(current.model_copy(update=update))
        return self._require_workflow(workflow_id)

    def invalidate_workflow(self, workflow_id: str, reason: str | None = None) -> Workflow:
        current = self._require_workflow(workflow_id)
        updated = current.model_copy(update={
            "status": WorkflowStatus.INVALID, "updated_at": utcnow(),
            "metadata": {**current.metadata, "invalidated_reason": reason},
        })
        self._store.replace_workflow(updated)
        return updated

    def forget_workflow(self, workflow_id: str) -> bool:
        deleted = self._store.delete_workflow(workflow_id)
        self._unindex("workflows", workflow_id)
        return deleted

    # ================================================================== maintenance

    def rebuild_semantic_index(self) -> dict[str, int]:
        """Re-embed everything from SQLite (the source of truth) into LanceDB."""
        if self._semantic is None:
            return {"memories": 0, "workflows": 0}
        memories = [(m.id, m.text()) for m in self._store.list_memories(limit=1_000_000)]
        workflows = [(w.id, w.text()) for w in self._store.list_workflows(limit=1_000_000)]
        for kind, items in (("memories", memories), ("workflows", workflows)):
            self._semantic.clear(kind)
            for i in range(0, len(items), 64):
                self._semantic.upsert_many(kind, items[i:i + 64])
        return {"memories": len(memories), "workflows": len(workflows)}

    # ================================================================== internals

    def _require_memory(self, memory_id: str) -> Memory:
        m = self._store.get_memory(memory_id)
        if m is None:
            raise MemoryNotFoundError(memory_id)
        return m

    def _require_workflow(self, workflow_id: str) -> Workflow:
        w = self._store.get_workflow(workflow_id)
        if w is None:
            raise MemoryNotFoundError(workflow_id)
        return w

    def _index(self, kind: str, item_id: str, text: str) -> None:
        if self._semantic is None:
            return
        try:
            self._semantic.upsert(kind, item_id, text)
        except Exception:
            # SQLite already holds the record; rebuild_semantic_index() can repair the index.
            logger.warning("semantic indexing failed for %s %s", kind, item_id, exc_info=True)

    def _unindex(self, kind: str, item_id: str) -> None:
        if self._semantic is None:
            return
        try:
            self._semantic.delete(kind, item_id)
        except Exception:
            logger.warning("semantic delete failed for %s %s", kind, item_id, exc_info=True)

    def _redact_memory(self, m: Memory) -> Memory:
        content = self._privacy.redact(m.content)
        meta = self._privacy.redact(m.metadata.model_dump(exclude={"redactions"}))
        kinds = content.kinds | meta.kinds
        if not kinds:
            return m
        logger.info("memory.redacted id=%s kinds=%s", m.id, sorted(kinds))
        metadata = MemoryMetadata.model_validate({**meta.value, "redactions": sorted(set(m.metadata.redactions) | kinds)})
        return m.model_copy(update={"content": content.value, "metadata": metadata})

    def _redact_workflow(self, w: Workflow) -> Workflow:
        data = w.model_dump(mode="json")
        kinds: set[str] = set()
        for step in data["steps"]:
            params = step["parameters"]
            if params.get("sensitive") and params.get("text") and params["text"] != marker("sensitive_input"):
                params["text"] = marker("sensitive_input")
                params["requires_user_input"] = True
                kinds.add("sensitive_input")
        result = self._privacy.redact(data)
        kinds |= result.kinds
        if not kinds:
            return w
        redacted = result.value
        redacted["metadata"]["redactions"] = sorted(set(w.metadata.get("redactions", [])) | kinds)
        logger.info("workflow.redacted id=%s kinds=%s", w.id, sorted(kinds))
        return Workflow.model_validate(redacted)

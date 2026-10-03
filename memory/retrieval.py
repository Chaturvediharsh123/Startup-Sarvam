"""Hybrid retrieval: FTS5 keyword candidates + LanceDB semantic candidates -> merge ->
deterministic rerank -> relevance filter -> context.

Ranking (v1, deliberately simple and explainable):

* keyword score  = share of meaningful query terms found in the record (prefix match)
* semantic score = cosine similarity rescaled so ``semantic_min_similarity`` maps to 0
* relevance      = max(kw, sem) + agreement_bonus * min(kw, sem), capped at 1
* records below ``min_relevance`` are dropped
* score          = relevance * ((1 - w) + w * confidence) * freshness weight

All weights live in ``MemorySettings.ranking`` and are tuned with ``memory.evaluation``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Iterable

from pydantic import BaseModel, Field

from memory.config import MemorySettings
from memory.models import Freshness, Memory, MemorySource, MemoryType, RetrievedMemory, as_utc, utcnow
from memory.semantic_store import SemanticStore
from memory.sqlite_store import SQLiteStore, tokenize
from memory.workflow import Workflow, WorkflowFreshness, WorkflowMatch
from memory.working import WorkingMemorySnapshot

logger = logging.getLogger("memory.retrieval")

STOPWORDS = frozenset({
    # English
    "a", "an", "the", "i", "me", "my", "mine", "we", "our", "you", "your", "it", "its", "is", "are", "was", "were",
    "be", "been", "to", "of", "in", "on", "for", "and", "or", "how", "what", "which", "when", "where", "who",
    "did", "do", "does", "done", "last", "time", "this", "that", "these", "those", "with", "from", "at", "by",
    "can", "could", "would", "should", "please", "again", "previous", "used", "use", "about", "there", "here",
    # Hinglish (romanized)
    "ka", "ki", "ke", "ko", "se", "mein", "mai", "main", "hai", "hain", "tha", "thi", "the", "kya", "kaise", "kab",
    "kahan", "mera", "meri", "mere", "maine", "pe", "par", "aur", "ye", "yeh", "woh", "wo", "karo", "kar", "do",
    "lo", "pichli", "baar", "wala", "wali", "tha", "jo", "bhi", "ab",
    # Hindi
    "का", "की", "के", "को", "से", "में", "है", "हैं", "था", "थी", "क्या", "कैसे", "कब", "मेरा", "मेरी", "मेरे",
    "मैंने", "पर", "और", "यह", "वह", "जो", "भी", "पिछली", "बार",
})

def query_terms(query: str) -> list[str]:
    tokens = list(dict.fromkeys(tokenize(query)))
    meaningful = [t for t in tokens if t not in STOPWORDS]
    return meaningful or tokens


def keyword_score(terms: list[str], text: str) -> float:
    if not terms:
        return 0.0
    doc = set(tokenize(text))
    hits = sum(1 for t in terms if t in doc or (len(t) >= 3 and any(d.startswith(t) for d in doc)))
    return hits / len(terms)


def _date(value: datetime | None) -> str:
    return value.date().isoformat() if value else "never"


def memory_caveat(m: Memory, freshness: Freshness) -> str | None:
    if freshness is Freshness.VERIFIED:
        return None
    if freshness is Freshness.EXPIRED:
        return f"Expired on {_date(m.expires_at)}; historical context only."
    if freshness is Freshness.STALE:
        if m.last_verified_at:
            return f"Last verified {_date(m.last_verified_at)}; may be out of date."
        return f"Recorded {_date(m.created_at)} and never verified; may be out of date."
    if freshness is Freshness.INVALID:
        return "Marked invalid."
    inferred = " (inferred, not stated by the user)" if m.source is MemorySource.AGENT else ""
    return f"Not verified yet{inferred}; confidence {m.confidence:.2f}."


def workflow_caveat(w: Workflow, freshness: WorkflowFreshness) -> str | None:
    last_ok = _date(w.last_verified_at)
    return {
        WorkflowFreshness.CURRENT: None,
        WorkflowFreshness.UNVERIFIED: f"Version {w.version} has not been verified yet; revalidate before relying on it.",
        WorkflowFreshness.STALE: f"Last worked on {last_ok}; the site/app may have changed. Revalidate each step.",
        WorkflowFreshness.FAILING: f"Most recent run failed (last success {last_ok}). Adapt before reuse.",
        WorkflowFreshness.EXPIRED: f"Expired on {_date(w.expires_at)}; historical context only.",
        WorkflowFreshness.INVALID: "Retired workflow.",
    }[freshness]


class MemoryContext(BaseModel):
    """What the planner receives: relevant memories, candidate workflows and current task state."""

    query: str
    memories: list[RetrievedMemory] = Field(default_factory=list)
    workflows: list[WorkflowMatch] = Field(default_factory=list)
    working: WorkingMemorySnapshot | None = None

    def to_prompt(self) -> str:
        lines: list[str] = []
        if self.working is not None:
            w = self.working
            if w.current_task:
                lines.append(f"Current task: {w.current_task}")
            if w.current_application:
                lines.append(f"Current application: {w.current_application}")
            if w.current_page:
                lines.append(f"Current page: {w.current_page.get('title') or ''} {w.current_page.get('url') or ''}".strip())
            for turn in w.recent_turns[-6:]:
                lines.append(f"[{turn['role']}] {turn['text']}")
            for r in w.recent_action_results[-5:]:
                status = "ok" if r.get("success") else f"failed ({r.get('error_type')})"
                lines.append(f"Recent action {r.get('action_type')}: {status}")
        if self.memories:
            lines.append("Relevant memories (context, not guaranteed truth):")
            for hit in self.memories:
                m = hit.memory
                text = m.content if isinstance(m.content, str) else m.text()
                tag = f"{m.type.value}, {m.source.value}, {hit.freshness.value}"
                lines.append(f"- [{tag}] {text}" + (f"  ({hit.caveat})" if hit.caveat else ""))
        if self.workflows:
            lines.append("Known workflows (check preconditions against the current screen before use):")
            for match in self.workflows:
                w = match.workflow
                lines.append(f"- {w.goal} v{w.version} [{match.freshness.value}, {w.success_count} ok / "
                             f"{w.failure_count} failed]" + (f"  ({match.caveat})" if match.caveat else ""))
        return "\n".join(lines)


class HybridRetriever:
    def __init__(self, store: SQLiteStore, semantic: SemanticStore | None, settings: MemorySettings) -> None:
        self.store = store
        self.semantic = semantic
        self.settings = settings

    def _candidates(self, kind: str, query: str) -> tuple[list[str], dict[str, float], dict[str, float | None]]:
        terms = query_terms(query)
        pool = self.settings.candidate_pool
        kw: dict[str, float] = {}
        for item_id, body in self.store.keyword_search(kind, terms, pool):
            kw[item_id] = keyword_score(terms, body)
        sem: dict[str, float | None] = {}
        if self.semantic is not None:
            try:
                floor = self.settings.semantic_min_similarity
                for item_id, similarity in self.semantic.search(kind, query, pool):
                    sem[item_id] = max(0.0, (similarity - floor) / (1.0 - floor))
            except Exception:
                logger.warning("semantic search failed; using keyword results only", exc_info=True)
        return terms, kw, sem

    def _relevance(self, kw: float, sem: float | None) -> float:
        s = sem or 0.0
        return min(1.0, max(kw, s) + self.settings.ranking.agreement_bonus * min(kw, s))

    def _confidence_factor(self, confidence: float) -> float:
        w = self.settings.ranking.confidence_weight
        return (1.0 - w) + w * confidence

    def search_memories(
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
        now = as_utc(now) or utcnow()
        stale_after = timedelta(days=self.settings.stale_after_days)
        type_filter = {MemoryType(t) for t in types} if types else None
        tag_filter = {t.casefold() for t in tags} if tags else None
        _, kw, sem = self._candidates("memories", query)
        records = self.store.get_memories([*kw, *sem])  # SQLite is the truth; orphan vectors are ignored

        hits: list[RetrievedMemory] = []
        for memory_id, m in records.items():
            freshness = m.freshness(now, stale_after)
            if freshness is Freshness.INVALID and not include_invalid:
                continue
            if freshness is Freshness.EXPIRED and not include_expired:
                continue
            if type_filter and m.type not in type_filter:
                continue
            if tag_filter and not tag_filter & {t.casefold() for t in m.metadata.tags}:
                continue
            k, s = kw.get(memory_id, 0.0), sem.get(memory_id)
            relevance = self._relevance(k, s)
            if relevance < self.settings.min_relevance:
                continue
            assert m.confidence is not None
            freshness_weight = self.settings.ranking.memory_freshness.get(freshness.value, 1.0)
            score = relevance * self._confidence_factor(m.confidence) * freshness_weight
            hits.append(RetrievedMemory(memory=m, score=round(score, 4), relevance=round(relevance, 4),
                                        keyword_score=round(k, 4), semantic_score=None if s is None else round(s, 4),
                                        freshness=freshness, caveat=memory_caveat(m, freshness)))
        hits.sort(key=lambda h: (-h.score, -h.memory.updated_at.timestamp(), h.memory.id))
        return hits[: limit or self.settings.default_limit]

    def search_workflows(
        self,
        query: str,
        *,
        limit: int = 3,
        include_invalid: bool = False,
        now: datetime | None = None,
    ) -> list[WorkflowMatch]:
        now = as_utc(now) or utcnow()
        stale_after = timedelta(days=self.settings.workflow_stale_after_days)
        _, kw, sem = self._candidates("workflows", query)
        matches: list[WorkflowMatch] = []
        for workflow_id, w in self.store.get_workflows([*kw, *sem]).items():
            freshness = w.freshness(now, stale_after)
            if freshness is WorkflowFreshness.INVALID and not include_invalid:
                continue
            relevance = self._relevance(kw.get(workflow_id, 0.0), sem.get(workflow_id))
            if relevance < self.settings.min_relevance:
                continue
            freshness_weight = self.settings.ranking.workflow_freshness.get(freshness.value, 1.0)
            score = relevance * self._confidence_factor(w.confidence) * freshness_weight
            matches.append(WorkflowMatch(workflow=w, score=round(score, 4), relevance=round(relevance, 4),
                                         freshness=freshness, caveat=workflow_caveat(w, freshness)))
        matches.sort(key=lambda m: (-m.score, -m.workflow.updated_at.timestamp(), m.workflow.id))
        return matches[:limit]

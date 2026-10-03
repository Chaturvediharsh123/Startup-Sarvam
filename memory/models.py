"""Memory records: facts, preferences, episodes and observations.

A memory is context, not truth. Every record carries provenance (``source``), ``confidence``
and lifecycle timestamps so retrieval can tell confirmed-current information apart from
stale, expired or merely inferred information. Workflows are a separate model
(``memory.workflow``), never a text blob in here.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """Naive datetimes are taken to be UTC; aware ones are converted to UTC."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class MemoryNotFoundError(KeyError):
    pass


class WorkflowNotVerifiedError(ValueError):
    """Raised when storing a workflow that has never been seen to succeed."""


class MemoryType(str, Enum):
    FACT = "fact"
    PREFERENCE = "preference"
    EPISODE = "episode"
    OBSERVATION = "observation"


class MemorySource(str, Enum):
    USER = "user"            # explicitly stated by the user
    AGENT = "agent"          # inferred by the planner/LLM
    SYSTEM = "system"        # configured by the application
    EXECUTION = "execution"  # observed while executing actions


class MemoryStatus(str, Enum):
    ACTIVE = "active"
    INVALID = "invalid"  # explicitly contradicted or retracted; kept for audit, excluded from search


class Freshness(str, Enum):
    """Derived (never stored) lifecycle state used by retrieval."""

    VERIFIED = "verified"      # verified within the staleness window
    UNVERIFIED = "unverified"  # recent but never verified
    STALE = "stale"            # verification (or creation, if never verified) is too old
    EXPIRED = "expired"        # past expires_at
    INVALID = "invalid"


DEFAULT_CONFIDENCE: dict[MemorySource, float] = {
    MemorySource.USER: 0.9,
    MemorySource.SYSTEM: 0.9,
    MemorySource.EXECUTION: 0.8,
    MemorySource.AGENT: 0.5,
}


class MemoryMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    tags: list[str] = Field(default_factory=list)
    application: str | None = None
    domain: str | None = None
    execution_id: str | None = None
    source_reference: str | None = None
    verification_method: str | None = None
    redactions: list[str] = Field(default_factory=list, description="Kinds of sensitive data removed before storage")


def flatten_content(content: str | dict[str, Any]) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []

    def walk(value: Any, key: str | None = None) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, str(k))
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v, key)
        elif value is not None:
            parts.append(f"{key}: {value}" if key else str(value))

    walk(content)
    return "\n".join(parts)


class Memory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"mem_{uuid.uuid4().hex}", pattern=r"^[A-Za-z0-9_-]{1,64}$")
    type: MemoryType
    content: str | dict[str, Any]
    source: MemorySource
    confidence: float | None = Field(default=None, ge=0, le=1, description="Defaults by source")
    status: MemoryStatus = MemoryStatus.ACTIVE
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    last_verified_at: datetime | None = None
    expires_at: datetime | None = None
    metadata: MemoryMetadata = Field(default_factory=MemoryMetadata)

    @field_validator("content")
    @classmethod
    def _non_empty(cls, v: str | dict[str, Any]) -> str | dict[str, Any]:
        if (isinstance(v, str) and not v.strip()) or (isinstance(v, dict) and not v):
            raise ValueError("content must not be empty")
        if isinstance(v, dict):
            json.dumps(v)  # must be JSON-serialisable for SQLite
        return v

    @field_validator("created_at", "updated_at", "last_verified_at", "expires_at")
    @classmethod
    def _utc(cls, v: datetime | None) -> datetime | None:
        return as_utc(v)

    @model_validator(mode="after")
    def _defaults(self) -> Memory:
        if self.confidence is None:
            self.confidence = DEFAULT_CONFIDENCE[self.source]
        return self

    def text(self) -> str:
        """Searchable text: content plus tags/application/domain."""
        extras = [*self.metadata.tags, self.metadata.application or "", self.metadata.domain or ""]
        return "\n".join(filter(None, [flatten_content(self.content), " ".join(e for e in extras if e)]))

    def freshness(self, now: datetime | None = None, stale_after: timedelta = timedelta(days=90)) -> Freshness:
        now = as_utc(now) or utcnow()
        if self.status is MemoryStatus.INVALID:
            return Freshness.INVALID
        if self.expires_at is not None and self.expires_at <= now:
            return Freshness.EXPIRED
        if self.last_verified_at is None:
            return Freshness.STALE if now - self.created_at > stale_after else Freshness.UNVERIFIED
        return Freshness.VERIFIED if now - self.last_verified_at <= stale_after else Freshness.STALE


class MemoryUpdate(BaseModel):
    """Partial update. Only fields that are explicitly set are applied; ``metadata`` is merged."""

    model_config = ConfigDict(extra="forbid")

    type: MemoryType | None = None
    content: str | dict[str, Any] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    expires_at: datetime | None = None
    metadata: dict[str, Any] | None = None


class MemoryVerification(BaseModel):
    memory_id: str
    verified_at: datetime
    method: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    notes: str | None = None


class RetrievedMemory(BaseModel):
    """A search hit. ``is_confirmed`` is True only for recently verified memories; anything
    else carries a ``caveat`` and must not be presented as current truth."""

    memory: Memory
    score: float
    relevance: float
    keyword_score: float
    semantic_score: float | None
    freshness: Freshness
    caveat: str | None = None
    search_id: str | None = Field(default=None, description="Set when retrieval logging is on; pass to record_search_feedback")

    @property
    def is_confirmed(self) -> bool:
        return self.freshness is Freshness.VERIFIED

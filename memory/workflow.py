"""Workflows: structured, executable procedural knowledge with verification history.

A workflow is not a memory. It has a goal, preconditions to check against the *current*
environment, ordered Action-compatible steps, expected outcomes, failure conditions and a
verification history that decides whether it is still current. It is stored only after a
verified successful execution and must never be replayed blindly.

The Memory Block does not import the Action Block: steps and conditions are plain data whose
shape matches ``actions.Action`` / ``actions.Condition`` (see ``to_action_payload``).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from memory.models import as_utc, utcnow


class WorkflowStatus(str, Enum):
    ACTIVE = "active"
    INVALID = "invalid"  # retired: known broken or superseded


class WorkflowFreshness(str, Enum):
    CURRENT = "current"        # last run succeeded within the staleness window
    UNVERIFIED = "unverified"  # current version has no successful run yet
    STALE = "stale"            # last success is too old
    FAILING = "failing"        # most recent run failed
    EXPIRED = "expired"
    INVALID = "invalid"


class WorkflowCondition(BaseModel):
    """Precondition, expected outcome or failure condition.

    ``description`` is always required (for the planner/verifier). ``kind``/``target``/``value``
    are optional machine-checkable parts compatible with ``actions.Condition``.
    """

    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=300)
    kind: str | None = None
    target: dict[str, Any] | None = None
    value: str | None = None

    def to_action_condition(self) -> dict[str, Any]:
        if not self.kind:
            return {"kind": "custom", "description": self.description}
        data = {"kind": self.kind, "target": self.target, "value": self.value, "description": self.description}
        return {k: v for k, v in data.items() if v is not None}


class WorkflowStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order: int = Field(ge=1)
    description: str = Field(min_length=1, max_length=300)
    action_type: str = Field(pattern=r"^[a-z_]{2,32}$")
    target: dict[str, Any] | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    expected_outcome: str | None = Field(default=None, max_length=300)
    expected_conditions: list[WorkflowCondition] = Field(default_factory=list)

    def to_action_payload(self) -> dict[str, Any]:
        """A dict the planner can hand to ``ActionExecutor.execute`` after adapting it to the
        current state. Redacted inputs (``requires_user_input``) must be filled in first."""
        payload: dict[str, Any] = {"type": self.action_type, "parameters": dict(self.parameters)}
        if self.target is not None:
            payload["target"] = self.target
        outcomes = [c.to_action_condition() for c in self.expected_conditions if c.kind]
        if outcomes:
            payload["expected_outcomes"] = outcomes
        return payload

    @property
    def requires_user_input(self) -> bool:
        return bool(self.parameters.get("requires_user_input"))


class WorkflowVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(min_length=1)
    timestamp: datetime = Field(default_factory=utcnow)
    success: bool
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    observed_changes: list[str] = Field(default_factory=list)
    notes: str | None = None
    workflow_version: int | None = None

    @field_validator("timestamp")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return as_utc(v)  # type: ignore[return-value]


PROCEDURAL_FIELDS = ("preconditions", "steps", "expected_outcomes", "failure_conditions")


class Workflow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"wf_{uuid.uuid4().hex}", pattern=r"^[A-Za-z0-9_-]{1,64}$")
    goal: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=2000)
    preconditions: list[WorkflowCondition] = Field(default_factory=list)
    steps: list[WorkflowStep] = Field(min_length=1)
    expected_outcomes: list[WorkflowCondition] = Field(default_factory=list, description="Final success conditions")
    failure_conditions: list[WorkflowCondition] = Field(default_factory=list)
    verification_history: list[WorkflowVerification] = Field(default_factory=list)
    source_execution_id: str | None = None
    confidence: float = Field(default=0.6, ge=0, le=1)
    status: WorkflowStatus = WorkflowStatus.ACTIVE
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    last_verified_at: datetime | None = None
    expires_at: datetime | None = None
    version: int = Field(default=1, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict, description="tags, application, domain, ...")

    @field_validator("created_at", "updated_at", "last_verified_at", "expires_at")
    @classmethod
    def _utc(cls, v: datetime | None) -> datetime | None:
        return as_utc(v)

    @model_validator(mode="after")
    def _ordered_steps(self) -> Workflow:
        orders = [s.order for s in self.steps]
        if len(set(orders)) != len(orders):
            raise ValueError("step orders must be unique")
        self.steps = sorted(self.steps, key=lambda s: s.order)
        self.verification_history = sorted(self.verification_history, key=lambda v: v.timestamp)
        return self

    # ------------------------------------------------------------------ derived state

    @property
    def current_version_history(self) -> list[WorkflowVerification]:
        return [v for v in self.verification_history if v.workflow_version in (None, self.version)]

    @property
    def last_verification(self) -> WorkflowVerification | None:
        """Most recent run of the *current* version."""
        history = self.current_version_history
        return history[-1] if history else None

    @property
    def success_count(self) -> int:
        return sum(v.success for v in self.verification_history)

    @property
    def failure_count(self) -> int:
        return sum(not v.success for v in self.verification_history)

    def freshness(self, now: datetime | None = None, stale_after: timedelta = timedelta(days=30)) -> WorkflowFreshness:
        now = as_utc(now) or utcnow()
        if self.status is WorkflowStatus.INVALID:
            return WorkflowFreshness.INVALID
        if self.expires_at is not None and self.expires_at <= now:
            return WorkflowFreshness.EXPIRED
        last = self.last_verification
        if last is not None and not last.success:
            return WorkflowFreshness.FAILING
        if self.last_verified_at is None:
            return WorkflowFreshness.UNVERIFIED
        return WorkflowFreshness.CURRENT if now - self.last_verified_at <= stale_after else WorkflowFreshness.STALE

    def text(self) -> str:
        meta = self.metadata
        tags = meta.get("tags") or []
        parts = [self.goal, self.description or "", *(s.description for s in self.steps),
                 " ".join(str(t) for t in tags), str(meta.get("application") or ""), str(meta.get("domain") or "")]
        return "\n".join(p for p in parts if p)

    def action_payloads(self) -> list[dict[str, Any]]:
        return [s.to_action_payload() for s in self.steps]


class WorkflowUpdate(BaseModel):
    """Partial update. Changing any procedural field creates a new version."""

    model_config = ConfigDict(extra="forbid")

    goal: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    preconditions: list[WorkflowCondition] | None = None
    steps: list[WorkflowStep] | None = Field(default=None, min_length=1)
    expected_outcomes: list[WorkflowCondition] | None = None
    failure_conditions: list[WorkflowCondition] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    expires_at: datetime | None = None
    status: WorkflowStatus | None = None
    metadata: dict[str, Any] | None = None


class WorkflowMatch(BaseModel):
    """A retrieved workflow. Use it as a starting point only when ``is_current`` and after
    re-checking its preconditions against the live environment."""

    workflow: Workflow
    score: float
    relevance: float
    freshness: WorkflowFreshness
    caveat: str | None = None

    @property
    def is_current(self) -> bool:
        return self.freshness is WorkflowFreshness.CURRENT

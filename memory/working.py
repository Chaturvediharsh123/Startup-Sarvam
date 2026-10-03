"""Working memory: short-lived, in-RAM state of the current task. Never persisted.

Holds the current task, application and page, recent conversation turns (so "ab isko band
karo" can resolve "isko"), recent observations, recent ActionResults and pending actions.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from memory.models import utcnow


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return dict(value)
    raise TypeError(f"expected a pydantic model or dict, got {type(value).__name__}")


def summarize_action_result(result: Any) -> dict[str, Any]:
    """Compact view of an ActionResult (passed as model or dict; Memory never imports Action)."""
    data = _as_dict(result)
    error = data.get("error") or {}
    output = data.get("output")
    if isinstance(output, dict) and isinstance(output.get("text"), str) and len(output["text"]) > 500:
        output = {**output, "text": output["text"][:500] + "…"}
    return {
        "action_id": data.get("action_id"),
        "action_type": data.get("action_type"),
        "success": data.get("success"),
        "error_type": error.get("error_type"),
        "error": error.get("message"),
        "output": output,
        "evidence": [{"type": e.get("type"), "value": e.get("value")} for e in data.get("evidence") or []][:10],
        "expected_outcomes": [c.get("status") for c in data.get("outcome_checks") or []],
        "finished_at": data.get("finished_at"),
    }


class WorkingMemorySnapshot(BaseModel):
    current_task: str | None = None
    current_application: str | None = None
    current_page: dict[str, Any] | None = None
    task_state: dict[str, Any] = Field(default_factory=dict)
    recent_turns: list[dict[str, Any]] = Field(default_factory=list)
    recent_observations: list[dict[str, Any]] = Field(default_factory=list)
    recent_action_results: list[dict[str, Any]] = Field(default_factory=list)
    pending_actions: list[dict[str, Any]] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utcnow)


class WorkingMemory:
    def __init__(self, max_items: int = 20) -> None:
        self._lock = threading.RLock()
        self._max = max_items
        self._turns: deque[dict[str, Any]] = deque(maxlen=max_items)
        self.clear(keep_turns=False)

    def clear(self, *, keep_turns: bool = True) -> None:
        """Reset task state. Conversation turns survive by default so follow-ups still resolve."""
        with self._lock:
            self._task: str | None = None
            self._application: str | None = None
            self._page: dict[str, Any] | None = None
            self._state: dict[str, Any] = {}
            self._observations: deque[dict[str, Any]] = deque(maxlen=self._max)
            self._results: deque[dict[str, Any]] = deque(maxlen=self._max)
            self._pending: deque[dict[str, Any]] = deque(maxlen=self._max)
            if not keep_turns:
                self._turns.clear()
            self._updated = utcnow()

    def _touch(self) -> None:
        self._updated = utcnow()

    # ------------------------------------------------------------------ task context

    def set_task(self, task: str | None, *, reset: bool = True) -> None:
        with self._lock:
            if reset:
                self.clear()
            self._task = task
            self._touch()

    def set_application(self, application: str | None) -> None:
        with self._lock:
            self._application = application
            self._touch()

    def set_page(self, url: str | None, title: str | None = None) -> None:
        with self._lock:
            self._page = None if url is None else {"url": url, "title": title}
            self._touch()

    def update_state(self, **values: Any) -> None:
        with self._lock:
            self._state.update(values)
            self._touch()

    def get_state(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._state.get(key, default)

    # ------------------------------------------------------------------ recent history

    def add_turn(self, role: str, text: str, **meta: Any) -> None:
        with self._lock:
            self._turns.append({"role": role, "text": text, "at": utcnow().isoformat(), **meta})
            self._touch()

    def add_observation(self, observation: str | dict[str, Any], source: str = "agent") -> None:
        with self._lock:
            body = {"text": observation} if isinstance(observation, str) else dict(observation)
            self._observations.append({**body, "source": source, "at": utcnow().isoformat()})
            self._touch()

    def record_action_result(self, result: Any) -> dict[str, Any]:
        summary = summarize_action_result(result)
        with self._lock:
            self._results.append(summary)
            self._pending = deque((p for p in self._pending if p.get("action_id") != summary["action_id"]), maxlen=self._max)
            self._touch()
        return summary

    def add_pending(self, action: Any) -> None:
        with self._lock:
            self._pending.append(_as_dict(action))
            self._touch()

    def clear_pending(self) -> None:
        with self._lock:
            self._pending.clear()
            self._touch()

    @property
    def last_action_result(self) -> dict[str, Any] | None:
        with self._lock:
            return self._results[-1] if self._results else None

    def snapshot(self) -> WorkingMemorySnapshot:
        with self._lock:
            return WorkingMemorySnapshot(
                current_task=self._task,
                current_application=self._application,
                current_page=dict(self._page) if self._page else None,
                task_state=dict(self._state),
                recent_turns=list(self._turns),
                recent_observations=list(self._observations),
                recent_action_results=list(self._results),
                pending_actions=list(self._pending),
                updated_at=self._updated,
            )

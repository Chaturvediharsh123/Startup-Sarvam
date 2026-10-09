"""Audit log (PDF "Safety"): every request, decision and result, one JSON line each.

Subscribe :meth:`AuditLog.handle` to the event bus. It records:

* ``request``  - a :class:`BrainResult` that carries a tool call
* ``decision`` - a :class:`SafetyDecision` (allow / confirm / deny and why)
* ``result``   - an :class:`ActionResult` (status, fact, duration)

Each line has the time and turn id. Long text is truncated and anything that looks
like a key, token or password is replaced by ``***``. :meth:`recent` returns the
last entries for the UI.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from core.config import Settings
from core.events import ActionResult, BrainResult, Event, SafetyDecision

logger = logging.getLogger(__name__)

AUDIT_FILE = "audit.jsonl"
REDACTED = "***"
_SECRET_KEY = re.compile(r"api[_-]?key|token|password|passwd|secret|authori[sz]ation|cookie", re.I)
_SECRET_VALUE = re.compile(r"\b(sk|key|token)[_-][A-Za-z0-9_\-]{8,}")


class AuditLog:
    """Append-only JSONL audit trail with an in-memory tail for the UI."""

    def __init__(
        self,
        path: Path,
        max_text: int = 200,
        keep: int = 500,
        max_bytes: int = 2_000_000,
        secrets: tuple[str, ...] = (),
    ) -> None:
        self.path = Path(path)
        self.max_text = max_text
        self.max_bytes = max_bytes
        self._secrets = tuple(s for s in secrets if s and len(s) >= 6)
        self._recent: deque[dict[str, Any]] = deque(maxlen=keep)
        self._lock = threading.Lock()
        self._load_tail()

    @classmethod
    def from_settings(cls, settings: Settings) -> AuditLog:
        """``<log_dir>/audit.jsonl``; the API key is always redacted."""
        return cls(settings.log_dir / AUDIT_FILE, secrets=(settings.sarvam_api_key,))

    # -- bus subscriber --------------------------------------------------------------

    def handle(self, event: Event) -> None:
        """Record a request, decision or result event; ignore others. Never raises."""
        try:
            entry = self._entry(event)
            if entry is not None:
                self.record(entry)
        except Exception:  # noqa: BLE001 - auditing must never break a turn
            logger.exception("Could not write the audit log")

    def record(self, entry: dict[str, Any]) -> None:
        """Append one entry (redacted and truncated) to the file and the tail."""
        clean = self._clean(entry)
        line = json.dumps(clean, ensure_ascii=False, default=str)
        with self._lock:
            self._recent.append(clean)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate()
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                logger.exception("Could not write %s", self.path)

    def recent(self, n: int = 20) -> list[dict[str, Any]]:
        """The last ``n`` entries, oldest first."""
        with self._lock:
            items = list(self._recent)
        return items[-n:] if n > 0 else []

    # -- internals ---------------------------------------------------------------------

    def _entry(self, event: Event) -> dict[str, Any] | None:
        base = {
            "time": datetime.fromtimestamp(event.ts).isoformat(timespec="milliseconds"),
            "turn_id": event.turn_id,
        }
        if isinstance(event, BrainResult):
            if event.tool_call is None:
                return None
            return {**base, "kind": "request", "tool": event.tool_call.name,
                    "args": event.tool_call.args, "language": event.language}
        if isinstance(event, SafetyDecision):
            return {**base, "kind": "decision", "tool": event.tool_call.name,
                    "args": event.clean_args or event.tool_call.args,
                    "verdict": str(event.verdict), "risk": event.risk, "reason": event.reason}
        if isinstance(event, ActionResult):
            return {**base, "kind": "result", "tool": event.name, "args": event.args,
                    "status": event.status, "fact": event.fact,
                    "duration_ms": event.duration_ms}
        return None

    def _clean(self, value: Any, key: str = "") -> Any:
        if key and _SECRET_KEY.search(key):
            return REDACTED
        if isinstance(value, dict):
            return {str(k): self._clean(v, str(k)) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self._clean(v) for v in value]
        if isinstance(value, str):
            text = _SECRET_VALUE.sub(REDACTED, value)
            for secret in self._secrets:
                text = text.replace(secret, REDACTED)
            if len(text) > self.max_text:
                text = f"{text[: self.max_text]}...(+{len(text) - self.max_text} chars)"
            return text
        if value is None or isinstance(value, bool | int | float):
            return value
        return self._clean(str(value))

    def _rotate(self) -> None:
        if self.path.is_file() and self.path.stat().st_size > self.max_bytes:
            self.path.replace(self.path.with_suffix(self.path.suffix + ".1"))

    def _load_tail(self) -> None:
        if not self.path.is_file():
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines[-(self._recent.maxlen or 0):]:
            try:
                self._recent.append(json.loads(line))
            except json.JSONDecodeError:
                continue

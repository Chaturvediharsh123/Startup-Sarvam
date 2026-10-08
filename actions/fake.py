"""A fake action runner for tests and ``--mock`` mode (PDF "Actions": fake.py).

It has the same tool schemas as the real runner, records every call and always
succeeds, so nothing on the PC ever changes. Pass ``outcomes`` to script a
different status or fact for a given action name.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from actions import registry
from core.events import ActionResult, ToolCall
from core.interfaces import Health

_INFO: dict[str, dict[str, Any]] = {
    "system_status": {
        "battery_percent": 80, "charging": True, "cpu_percent": 10,
        "memory_percent": 40, "disk_free_gb": 100.0, "disk_percent": 50,
    },
}


class FakeActionRunner:
    """Records calls and always succeeds (unless an outcome is scripted)."""

    def __init__(self, outcomes: dict[str, tuple[str, str]] | None = None) -> None:
        """``outcomes`` maps an action name to ``(status, fact)`` to return instead of ok."""
        self.calls: list[ToolCall] = []
        self.outcomes = dict(outcomes or {})
        self._lock = threading.Lock()

    def start(self) -> None:
        """Nothing to start."""

    def stop(self) -> None:
        """Nothing to stop."""

    def health(self) -> Health:
        """Always healthy."""
        return Health("actions", True, "fake")

    def tool_schemas(self) -> list[dict[str, Any]]:
        """The same tool definitions as the real registry."""
        return registry.tool_schemas()

    def run(self, call: ToolCall, timeout_s: float | None = None) -> ActionResult:
        """Record ``call`` and return a successful (or scripted) result."""
        with self._lock:
            self.calls.append(call)
        spec = registry.ACTIONS.get(call.name)
        args = dict(call.args) if isinstance(call.args, dict) else {}
        if call.name in self.outcomes:
            status, fact = self.outcomes[call.name]
            return ActionResult(call.name, args, status, fact, fact=fact,
                                informational=bool(spec and spec.informational))
        value: Any = f"fake {call.name.replace('_', ' ')} done"
        if call.name == "current_time":
            now = datetime.now()
            value = {"time": now.strftime("%H:%M"), "date": now.strftime("%A, %d %B %Y")}
        elif call.name in _INFO:
            value = dict(_INFO[call.name])
        fact = spec.to_fact(value) if spec else str(value)
        return ActionResult(call.name, args, "ok", value, fact=fact,
                            informational=bool(spec and spec.informational))

    @property
    def names(self) -> list[str]:
        """Names of the recorded calls, in order."""
        return [c.name for c in self.calls]

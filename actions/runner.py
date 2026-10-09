"""Run one approved tool call off the caller's thread, with a timeout (PDF "Actions").

:class:`ActionRunner` implements :class:`core.interfaces.ActionRunner`. It never
raises: unknown actions, bad arguments, exceptions, the pyautogui emergency stop
and timeouts all come back as a :class:`core.events.ActionResult` with a short
English ``fact`` (e.g. ``"notepad not installed"``) the brain can phrase.

The runner does NOT decide whether an action is allowed; the safety guard does.
Pass it ``ToolCall(name, decision.clean_args)`` only after the guard said ALLOW
(or the user said yes).
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from actions import registry
from actions.registry import ACTIONS, ActionError
from core.config import Settings
from core.events import ActionResult, ToolCall
from core.interfaces import Health

logger = logging.getLogger(__name__)

MODULE = "actions"


class ActionRunner:
    """Runs registered actions in worker threads with a timeout. Never raises."""

    def __init__(self, settings: Settings | None = None, max_workers: int = 4) -> None:
        self._settings = settings
        self._max_workers = max_workers
        self._pool: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        self._halted = ""
        self._last_error = ""
        if settings is not None:
            registry.configure(settings)

    # -- lifecycle --------------------------------------------------------------

    def start(self) -> None:
        """Create the worker pool and scan the Start Menu once."""
        self._ensure_pool()
        from actions import apps

        try:
            apps.scan_start_menu()
        except Exception:  # noqa: BLE001 - a failed scan only disables install checks
            logger.exception("Start Menu scan failed")

    def stop(self) -> None:
        """Stop accepting work. Running actions finish in the background."""
        with self._lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def health(self) -> Health:
        """OK unless halted by the emergency stop or the last action failed badly."""
        if self._halted:
            return Health(MODULE, False, f"stopped: {self._halted}")
        return Health(MODULE, not self._last_error, self._last_error)

    # -- emergency stop ------------------------------------------------------------

    def halt(self, reason: str = "emergency stop") -> None:
        """Refuse every action until :meth:`resume` (wired to the kill switch)."""
        self._halted = reason or "emergency stop"
        logger.warning("Actions halted: %s", self._halted)

    def resume(self) -> None:
        """Allow actions again after :meth:`halt`."""
        self._halted = ""

    @property
    def halted(self) -> bool:
        """True after :meth:`halt` until :meth:`resume`."""
        return bool(self._halted)

    # -- ActionRunner protocol ------------------------------------------------------

    def tool_schemas(self) -> list[dict[str, Any]]:
        """OpenAI-style tool definitions generated from the registry."""
        return registry.tool_schemas()

    def run(self, call: ToolCall, timeout_s: float | None = None) -> ActionResult:
        """Run one tool call with a timeout; every problem becomes a result, never an error."""
        start = time.perf_counter()
        args = dict(call.args) if isinstance(call.args, dict) else {}
        spec = ACTIONS.get(call.name)
        if spec is None:
            return self._result(call, args, "error", None, f"unknown action {call.name}", start)
        if self._halted:
            return self._result(
                call, args, "cancelled", None, "stopped by the emergency stop", start, spec
            )
        limit = timeout_s if timeout_s is not None else self._timeout()
        try:
            future: Future[Any] = self._ensure_pool().submit(spec.func, **args)
        except Exception as exc:  # noqa: BLE001 - e.g. pool shut down
            return self._result(call, args, "error", None, "actions are not running", start,
                                spec, exc)
        try:
            value = future.result(timeout=limit)
        except FutureTimeout:
            future.cancel()
            logger.warning("Action %s timed out after %.1f s", call.name, limit)
            return self._result(call, args, "timeout", None,
                                f"{_label(call.name)} took too long", start, spec)
        except ActionError as exc:
            return self._result(call, args, "error", None, str(exc), start, spec, exc)
        except TypeError as exc:
            return self._result(call, args, "error", None,
                                f"{_label(call.name)} got bad arguments", start, spec, exc)
        except Exception as exc:  # noqa: BLE001 - any action failure is reported, not raised
            if type(exc).__name__ == "FailSafeException":
                return self._result(call, args, "cancelled", None,
                                    "stopped by the emergency stop", start, spec, exc)
            logger.exception("Action %s failed", call.name)
            return self._result(call, args, "error", None, f"{_label(call.name)} failed",
                                start, spec, exc)
        try:
            fact = spec.to_fact(value)
        except Exception:  # noqa: BLE001 - a fact formatter bug must not lose the result
            fact = f"{_label(call.name)} done"
        self._last_error = ""
        return self._result(call, args, "ok", value, fact, start, spec)

    # -- helpers -------------------------------------------------------------------

    def _timeout(self) -> float:
        settings = self._settings or registry.config()
        return float(settings.action_timeout_s)

    def _ensure_pool(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(
                    max_workers=self._max_workers, thread_name_prefix="action"
                )
            return self._pool

    def _result(
        self,
        call: ToolCall,
        args: dict[str, Any],
        status: str,
        value: Any,
        fact: str,
        start: float,
        spec: registry.ActionSpec | None = None,
        exc: BaseException | None = None,
    ) -> ActionResult:
        extra: dict[str, Any] = {}
        if exc is not None:
            extra["error"] = f"{type(exc).__name__}: {exc}"[:300]
        if status in ("error", "timeout") and exc is not None and not isinstance(exc, ActionError):
            self._last_error = fact
        if call.call_id:
            extra["call_id"] = call.call_id
        return ActionResult(
            name=call.name,
            args=args,
            status=status,
            result=value if status == "ok" else fact,
            duration_ms=int((time.perf_counter() - start) * 1000),
            informational=bool(spec and spec.informational),
            extra=extra,
            fact=fact,
        )


def _label(name: str) -> str:
    return name.replace("_", " ")

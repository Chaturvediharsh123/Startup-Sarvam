"""The safety guard (PDF "Safety"): allow, ask first, or deny every tool call.

:class:`SafetyGuard` implements :class:`core.interfaces.Guard`. Order of checks:

1. Emergency stop: after the kill switch fired, everything is denied.
2. Allow-list: an action not in ``config/allowlist.yaml`` (or with no registered
   function) is denied "not on the allow-list".
3. Arguments: types against the action's JSON schema, then the allow-list rules
   (app must resolve to an allowed app, shortcut must be safe and not blocked,
   text within limits). Failures are denied "bad arguments: ...".
4. Rate limit: at most N actions per minute; over the limit is denied "rate limit".
5. Risk: low = allow, medium = allow and log, high = confirm (a spoken question in
   the user's language; only a clear yes runs it).

The guard never runs an action. It only returns a :class:`SafetyDecision` with
cleaned arguments (e.g. ``"crome"`` becomes ``"chrome"``) for the runner.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from core.config import Settings
from core.events import ActionResult, SafetyDecision, ToolCall, Verdict
from core.interfaces import Health
from core.logging import ACTIONS_LOGGER
from safety.allowlist import Allowlist
from safety.confirm import AskUser, confirmation_question, is_no, is_yes

__all__ = [
    "ActionResult",  # re-exported for older imports (ui/app.py)
    "ArgumentError",
    "AskUser",
    "RateLimiter",
    "SafetyGuard",
    "is_no",
    "is_yes",
    "validate_args",
]

logger = logging.getLogger(__name__)
audit = logging.getLogger(ACTIONS_LOGGER)

Schemas = Iterable[dict[str, Any]]
SchemasProvider = Schemas | Callable[[], Schemas]

NOT_ALLOWED = "not on the allow-list"
RATE_LIMIT = "rate limit"


class ArgumentError(ValueError):
    """Raised when LLM-supplied arguments do not match the schema or the allow-list."""


class RateLimiter:
    """Allow at most ``max_per_minute`` events in any 60-second window."""

    def __init__(self, max_per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.max_per_minute = max_per_minute
        self._clock = clock
        self._events: deque[float] = deque()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        """Record an event and return True, or return False if over the limit."""
        with self._lock:
            now = self._clock()
            while self._events and now - self._events[0] >= 60:
                self._events.popleft()
            if len(self._events) >= self.max_per_minute:
                return False
            self._events.append(now)
            return True


# ---------------------------------------------------------------------------
# JSON-schema argument validation (types, enums, ranges, required, no extras)
# ---------------------------------------------------------------------------


def _coerce_integer(key: str, value: Any) -> int:
    if isinstance(value, bool):
        raise ArgumentError(f"{key} must be a whole number")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    raise ArgumentError(f"{key} must be a whole number, got {value!r}")


def _check_value(key: str, value: Any, schema: dict[str, Any]) -> Any:
    """Validate (and lightly coerce) one argument against its JSON schema."""
    kind = schema.get("type")
    if kind == "integer":
        value = _coerce_integer(key, value)
        if "minimum" in schema and value < schema["minimum"]:
            raise ArgumentError(f"{key} must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ArgumentError(f"{key} must be at most {schema['maximum']}")
    elif kind == "string":
        if not isinstance(value, str):
            raise ArgumentError(f"{key} must be text")
        value = value.strip()
        if len(value) < schema.get("minLength", 0):
            raise ArgumentError(f"{key} must not be empty")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ArgumentError(f"{key} is too long (max {schema['maxLength']} characters)")
    elif kind == "boolean" and not isinstance(value, bool):
        raise ArgumentError(f"{key} must be true or false")
    if "enum" in schema and value not in schema["enum"]:
        raise ArgumentError(f"{key} must be one of {schema['enum']}, got {value!r}")
    return value


def validate_args(parameters: dict[str, Any], args: Any) -> dict[str, Any]:
    """Return arguments checked against a JSON-schema ``parameters`` object.

    Raises:
        ArgumentError: not an object, unknown or missing keys, wrong types or ranges.
    """
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ArgumentError("arguments must be an object")
    properties: dict[str, Any] = parameters.get("properties", {})
    extra = set(args) - set(properties)
    if extra:
        raise ArgumentError(f"unexpected arguments: {sorted(map(str, extra))}")
    missing = [k for k in parameters.get("required", []) if k not in args]
    if missing:
        raise ArgumentError(f"missing arguments: {missing}")
    return {key: _check_value(key, value, properties[key]) for key, value in args.items()}


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


class SafetyGuard:
    """Allow-list, argument checks, rate limit and risk level for every tool call."""

    def __init__(
        self,
        settings: Settings,
        allowlist_path: Path | None = None,
        schemas_provider: SchemasProvider | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        Args:
            settings: reads ``max_actions_per_minute`` and ``allowlist_path``.
            allowlist_path: overrides ``settings.allowlist_path``.
            schemas_provider: the tool schemas (a list, or a function returning one),
                e.g. ``ActionRunner.tool_schemas``. Safety never imports actions.
        """
        self.settings = settings
        self.allowlist = Allowlist.load(allowlist_path or settings.allowlist_path)
        self._provider = schemas_provider
        self._schemas: dict[str, dict[str, Any]] | None = None
        self.limiter = RateLimiter(settings.max_actions_per_minute, clock)
        self._halted = ""

    # -- lifecycle -----------------------------------------------------------------

    def start(self) -> None:
        """Nothing to open; the allow-list was read at construction."""

    def stop(self) -> None:
        """Nothing to close."""

    def health(self) -> Health:
        """Not OK while the emergency stop is active."""
        if self._halted:
            return Health("safety", False, f"stopped: {self._halted}")
        return Health("safety", True, f"{len(self.allowlist.actions)} actions allowed")

    def halt(self, reason: str = "emergency stop") -> None:
        """Deny every call until :meth:`resume` (wire the kill switch to this)."""
        self._halted = reason or "emergency stop"
        audit.warning("Emergency stop: %s", self._halted)

    def resume(self) -> None:
        """Allow calls again after :meth:`halt`."""
        self._halted = ""

    # -- Guard protocol ----------------------------------------------------------------

    def is_yes(self, text: str | None) -> bool:
        """True only for a clear yes ("haan", "yes", "aam", "avunu", ...)."""
        return is_yes(text)

    def is_no(self, text: str | None) -> bool:
        """True when the answer contains a no word."""
        return is_no(text)

    def check(
        self,
        call: ToolCall,
        language: str | None = None,
        romanised: bool = False,
        *,
        turn_id: str = "",
    ) -> SafetyDecision:
        """Allow, confirm or deny one tool call. Never raises."""
        try:
            return self._check(call, language, romanised, turn_id)
        except Exception as exc:  # noqa: BLE001 - a guard bug must deny, never crash
            logger.exception("Safety check failed for %s", getattr(call, "name", "?"))
            return self._decide(call, Verdict.DENY, f"safety check failed: {exc}", turn_id)

    # -- internals -------------------------------------------------------------------

    def _check(
        self, call: ToolCall, language: str | None, romanised: bool, turn_id: str
    ) -> SafetyDecision:
        if self._halted:
            return self._decide(call, Verdict.DENY, f"emergency stop: {self._halted}", turn_id)
        rule = self.allowlist.actions.get(call.name)
        schemas = self._schema_map()
        if rule is None or (schemas is not None and call.name not in schemas):
            return self._decide(call, Verdict.DENY, NOT_ALLOWED, turn_id)
        try:
            clean = self._clean_args(call.name, call.args, schemas)
        except ArgumentError as exc:
            return self._decide(call, Verdict.DENY, f"bad arguments: {exc}", turn_id, rule.risk)
        if not self.limiter.allow():
            reason = f"{RATE_LIMIT}: more than {self.limiter.max_per_minute} actions in a minute"
            return self._decide(call, Verdict.DENY, reason, turn_id, rule.risk, clean)
        if rule.risk == "high":
            question = confirmation_question(call.name, clean, language, romanised)
            return self._decide(call, Verdict.CONFIRM, "high risk: needs a spoken yes", turn_id,
                                rule.risk, clean, question)
        if rule.risk == "medium":
            audit.info("medium-risk action allowed: %s %s", call.name, _short(clean))
            return self._decide(call, Verdict.ALLOW, "medium risk: logged", turn_id,
                                rule.risk, clean)
        return self._decide(call, Verdict.ALLOW, "low risk", turn_id, rule.risk, clean)

    def _schema_map(self) -> dict[str, dict[str, Any]] | None:
        if self._provider is None:
            return None
        if self._schemas is None:
            raw = self._provider() if callable(self._provider) else self._provider
            self._schemas = {}
            for tool in raw:
                fn = tool.get("function", tool)
                self._schemas[str(fn["name"])] = dict(fn.get("parameters") or {})
        return self._schemas

    def _clean_args(
        self, name: str, args: Any, schemas: dict[str, dict[str, Any]] | None
    ) -> dict[str, Any]:
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise ArgumentError("arguments must be an object")
        rules = self.allowlist.actions[name].args
        extra = set(args) - set(rules)
        if extra:
            raise ArgumentError(f"unexpected arguments: {sorted(map(str, extra))}")
        canonical = {key: self._apply_rule(key, value, rules[key]) for key, value in args.items()}
        if schemas is None:
            return canonical
        return validate_args(schemas[name], canonical)

    def _apply_rule(self, key: str, value: Any, rule: dict[str, Any]) -> Any:
        """Check one argument against its allow-list rule; return its canonical value."""
        kind = rule.get("kind")
        allow = self.allowlist
        if kind in ("app", "closable_app"):
            if not isinstance(value, str) or not value.strip():
                raise ArgumentError(f"{key} must be an app name")
            app = allow.resolve_app(value)
            if app is None:
                raise ArgumentError(f"{value.strip()[:60]!r} is not an allowed app")
            if kind == "closable_app" and not allow.closable(app):
                raise ArgumentError(f"{app.replace('_', ' ')} cannot be closed")
            return app
        if kind == "shortcut":
            if not isinstance(value, str) or not value.strip():
                raise ArgumentError(f"{key} must be a shortcut name")
            try:
                return allow.shortcut(value)
            except ValueError as exc:
                raise ArgumentError(str(exc)) from exc
        if kind == "text":
            if not isinstance(value, str) or not value.strip():
                raise ArgumentError(f"{key} must be non-empty text")
            maximum = allow.limit(str(rule.get("max", "text_max_chars")), 2000)
            if len(value.strip()) > maximum:
                raise ArgumentError(f"{key} is too long (max {maximum} characters)")
            return value
        if kind == "choice":
            values = [str(v) for v in rule.get("values") or ()]
            text = str(value).strip().lower() if isinstance(value, str) else value
            if text not in values:
                raise ArgumentError(f"{key} must be one of {values}, got {value!r}")
            return text
        if kind == "int":
            number = _coerce_integer(key, value)
            low, high = rule.get("min"), rule.get("max")
            if (low is not None and number < low) or (high is not None and number > high):
                raise ArgumentError(f"{key} must be between {low} and {high}")
            return number
        raise ArgumentError(f"{key} has no allow-list rule")

    def _decide(
        self,
        call: ToolCall,
        verdict: Verdict,
        reason: str,
        turn_id: str,
        risk: str = "high",
        clean: dict[str, Any] | None = None,
        question: str = "",
    ) -> SafetyDecision:
        if verdict is Verdict.DENY:
            audit.info("denied %s: %s", getattr(call, "name", "?"), reason)
        return SafetyDecision(
            verdict=verdict,
            tool_call=call,
            reason=reason,
            question=question,
            risk=risk,
            clean_args=dict(clean or {}),
            turn_id=turn_id,
        )


def _short(args: dict[str, Any], limit: int = 80) -> dict[str, Any]:
    """Arguments with long text cut, for log lines."""
    return {k: (v[:limit] + "...") if isinstance(v, str) and len(v) > limit else v
            for k, v in args.items()}

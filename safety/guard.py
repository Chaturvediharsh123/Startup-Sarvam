"""The safety gate. :func:`execute` is the only way an action may run.

Order of checks for every call:

1. Allow-list: unknown action names are ``blocked``.
2. Argument validation against the action's JSON schema (types, enums, ranges,
   required keys, no extra keys). Failures are ``error``.
3. Rate limit: at most N actions per minute, so a looping LLM cannot spam the PC.
   Over the limit is ``blocked``.
4. Risky actions ask the user in their language and need a clear yes, otherwise
   ``denied``. The action function is never called without that yes.
5. Run with timing and exception capture. Exceptions are ``error``.
6. Log to memory and ``logs/actions.log``.
"""

from __future__ import annotations

import logging
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from actions.registry import ACTIONS, ActionSpec
from brain.memory import Memory
from core.logging import ACTIONS_LOGGER
from language.phrases import (
    NO_WORDS,
    PHRASES,
    QUESTION_WORDS,
    YES_PHRASES,
    YES_WORDS,
    phrase,
)

logger = logging.getLogger(__name__)
audit = logging.getLogger(ACTIONS_LOGGER)

AskUser = Callable[[str], str | None]
MAX_YES_TOKENS = 8


@dataclass
class ActionResult:
    """Outcome of one :func:`execute` call."""

    name: str
    args: dict[str, Any]
    status: str  # ok | error | denied | blocked
    result: Any
    duration_ms: int = 0
    informational: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when the action ran successfully."""
        return self.status == "ok"


class ArgumentError(ValueError):
    """Raised when LLM-supplied arguments do not match the action schema."""


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


_limiter = RateLimiter(10)


def configure(max_actions_per_minute: int) -> None:
    """Replace the module rate limiter (called once at start-up)."""
    global _limiter
    _limiter = RateLimiter(max_actions_per_minute)


# ---------------------------------------------------------------------------
# Yes / no detection
# ---------------------------------------------------------------------------


def _tokens(text: str) -> list[str]:
    """Lower-case words with punctuation and symbols removed (keeps Indic vowel signs)."""
    norm = unicodedata.normalize("NFC", text).lower().replace("’", "'")
    cleaned = "".join(
        " " if c != "'" and unicodedata.category(c)[0] in ("P", "S") else c for c in norm
    )
    return cleaned.split()


def is_yes(text: str | None) -> bool:
    """True only for a clear "yes" in a supported language.

    Any "no" word anywhere, silence, ``None``, noise, a question, or a long
    rambling answer counts as False.
    """
    if not text or not text.strip():
        return False
    if "?" in text or "？" in text:
        return False
    tokens = _tokens(text)
    if not tokens or len(tokens) > MAX_YES_TOKENS:
        return False
    if any(t in NO_WORDS or t in QUESTION_WORDS for t in tokens):
        return False
    joined = f" {' '.join(tokens)} "
    if any(f" {p} " in joined for p in YES_PHRASES):
        return True
    return any(t in YES_WORDS for t in tokens)


# ---------------------------------------------------------------------------
# Argument validation
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


def validate_args(spec: ActionSpec, args: Any) -> dict[str, Any]:
    """Return validated arguments for ``spec`` or raise :class:`ArgumentError`."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ArgumentError("arguments must be an object")
    properties: dict[str, Any] = spec.parameters.get("properties", {})
    extra = set(args) - set(properties)
    if extra:
        raise ArgumentError(f"unexpected arguments: {sorted(extra)}")
    missing = [k for k in spec.parameters.get("required", []) if k not in args]
    if missing:
        raise ArgumentError(f"missing arguments: {missing}")
    return {key: _check_value(key, value, properties[key]) for key, value in args.items()}


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def confirmation_question(
    spec: ActionSpec, args: dict[str, Any], language: str | None, romanised: bool
) -> str:
    """The spoken question asked before a risky action, in the user's language."""
    key = f"confirm_{spec.name}"
    if key not in PHRASES:
        return phrase("confirm_generic", language, romanised, name=spec.name.replace("_", " "))
    return phrase(key, language, romanised, name=args.get("name", ""))


def _record(memory: Memory | None, outcome: ActionResult) -> ActionResult:
    """Write the outcome to the database and the audit log."""
    text = outcome.result if isinstance(outcome.result, str) else repr(outcome.result)
    audit.info(
        "%s status=%s args=%s result=%s duration_ms=%d",
        outcome.name, outcome.status, outcome.args, text, outcome.duration_ms,
    )
    if memory is not None:
        try:
            memory.log_action(outcome.name, outcome.args, text, outcome.status, outcome.duration_ms)
        except Exception:  # noqa: BLE001 - logging must never break the turn
            logger.exception("Could not store action %s in memory", outcome.name)
    return outcome


def _confirmed(question: str, ask_user: AskUser | None) -> bool:
    """Ask the user; anything other than a clear yes (or any failure) is a no."""
    if ask_user is None:
        return False
    try:
        answer = ask_user(question)
    except Exception:  # noqa: BLE001 - a broken prompt must count as "no"
        logger.exception("ask_user failed; treating as no")
        return False
    logger.info("Confirmation answer: %r", answer)
    return is_yes(answer)


def _run(spec: ActionSpec, args: dict[str, Any]) -> ActionResult:
    """Call the action function with timing; exceptions become ``error`` results."""
    start = time.perf_counter()
    try:
        result = spec.func(**args)
        status = "ok"
    except Exception as exc:  # noqa: BLE001 - any action failure is reported, not raised
        logger.exception("Action %s failed", spec.name)
        result, status = f"{type(exc).__name__}: {exc}", "error"
    duration = int((time.perf_counter() - start) * 1000)
    return ActionResult(spec.name, args, status, result, duration, spec.informational)


def execute(
    name: str,
    args: Any,
    memory: Memory | None,
    ask_user: AskUser | None,
    language: str | None = None,
    romanised: bool = False,
) -> ActionResult:
    """Run one action through every safety check. Never raises for action problems."""
    spec = ACTIONS.get(name)
    safe_args = args if isinstance(args, dict) else {"raw": repr(args)}
    if spec is None:
        return _record(memory, ActionResult(name, safe_args, "blocked", "not on the allow-list"))
    try:
        clean = validate_args(spec, args)
    except ArgumentError as exc:
        return _record(memory, ActionResult(name, safe_args, "error", f"bad arguments: {exc}"))
    if not _limiter.allow():
        outcome = ActionResult(name, clean, "blocked", "rate limit reached")
        outcome.extra["rate_limited"] = True
        return _record(memory, outcome)
    if spec.risky:
        question = confirmation_question(spec, clean, language, romanised)
        if not _confirmed(question, ask_user):
            return _record(memory, ActionResult(name, clean, "denied", "user did not confirm"))
    return _record(memory, _run(spec, clean))


def enable_failsafe() -> None:
    """Turn on the pyautogui failsafe: moving the mouse to a corner stops automation."""
    import pyautogui

    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05

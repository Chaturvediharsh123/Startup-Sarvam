"""Logging configuration: console output plus rotating log files in ``log_dir``.

* ``logs/app.log`` receives every record from the application.
* ``logs/actions.log`` receives only records from the ``actions`` logger, which
  the safety audit uses for every PC action.
* Every record carries the current turn id (``[t0007-...]``) so one user sentence
  can be traced across modules, and API keys are masked before anything is written.
"""

from __future__ import annotations

import contextvars
import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

ACTIONS_LOGGER = "actions"
_FORMAT = "%(asctime)s %(levelname)-7s [%(turn_id)s] %(name)s: %(message)s"

current_turn: contextvars.ContextVar[str] = contextvars.ContextVar("current_turn", default="-")

_SECRET = re.compile(
    r"(sk_[A-Za-z0-9_\-]{6})[A-Za-z0-9_\-]+"
    r"|(api-subscription-key['\"]?\s*[:=]\s*['\"]?)[^'\",\s}]+",
    re.IGNORECASE,
)


def mask_secrets(text: str) -> str:
    """Hide Sarvam API keys and auth headers in log text."""
    return _SECRET.sub(lambda m: (m.group(1) or m.group(2)) + "***", text)


class TurnContextFilter(logging.Filter):
    """Adds ``record.turn_id`` and masks secrets in the message."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Always keep the record; only decorate it."""
        if not hasattr(record, "turn_id"):
            record.turn_id = current_turn.get()
        if isinstance(record.msg, str) and ("sk_" in record.msg or "subscription" in record.msg):
            record.msg = mask_secrets(record.msg)
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(mask_secrets(a) if isinstance(a, str) else a for a in record.args)
        return True


_TURN_FILTER = TurnContextFilter()
_MAX_BYTES = 1_000_000
_BACKUPS = 3
_MARKER = "_sarvam_handler"


def _file_handler(path: Path, level: int) -> RotatingFileHandler:
    """Create a UTF-8 rotating file handler (Hindi and Tamil text must survive)."""
    handler = RotatingFileHandler(path, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.addFilter(_TURN_FILTER)
    setattr(handler, _MARKER, True)
    return handler


def _remove_own_handlers(logger: logging.Logger) -> None:
    """Remove handlers added by an earlier call so setup can run twice safely."""
    for handler in list(logger.handlers):
        if getattr(handler, _MARKER, False):
            logger.removeHandler(handler)
            handler.close()


def setup_logging(log_dir: Path, level: int = logging.INFO, console: bool = True) -> None:
    """Configure the root logger and the ``actions`` audit logger.

    Args:
        log_dir: folder for ``app.log`` and ``actions.log``; created if missing.
        level: minimum level for the console and ``app.log``.
        console: also log to stderr. Turned off for the terminal text mode so the
            conversation stays readable.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    _remove_own_handlers(root)
    root.setLevel(min(level, logging.INFO))
    root.addHandler(_file_handler(log_dir / "app.log", level))
    if console:
        stream = logging.StreamHandler()
        stream.setLevel(level)
        stream.setFormatter(logging.Formatter("%(levelname)s [%(turn_id)s] %(name)s: %(message)s"))
        stream.addFilter(_TURN_FILTER)
        setattr(stream, _MARKER, True)
        root.addHandler(stream)

    actions = logging.getLogger(ACTIONS_LOGGER)
    _remove_own_handlers(actions)
    actions.setLevel(logging.INFO)
    actions.addHandler(_file_handler(log_dir / "actions.log", logging.INFO))

    for noisy in ("httpx", "httpcore", "websockets", "comtypes", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def shutdown_logging() -> None:
    """Flush and close the handlers this module added."""
    _remove_own_handlers(logging.getLogger())
    _remove_own_handlers(logging.getLogger(ACTIONS_LOGGER))

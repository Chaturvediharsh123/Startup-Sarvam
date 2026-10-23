"""Protocols every module implements (PDF section "Interfaces").

Modules import only from ``core``. The orchestrator talks to modules through
these Protocols, so a real module and its fake are interchangeable.

This file is a frozen contract: changing it needs the tech lead's review.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from core.events import ActionResult, BrainResult, SafetyDecision, ToolCall

AudioReader = Callable[[float], bytes | None]
"""Returns the next mic chunk, waiting up to the given seconds, or ``None``."""


class CancelToken:
    """Set when the user interrupts a turn. Every long step checks it."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self.reason = ""

    def cancel(self, reason: str = "user") -> None:
        """Mark the turn as cancelled."""
        self.reason = reason
        self._event.set()

    @property
    def cancelled(self) -> bool:
        """True once :meth:`cancel` was called."""
        return self._event.is_set()

    def wait(self, timeout: float) -> bool:
        """Sleep up to ``timeout`` seconds; return True early if cancelled."""
        return self._event.wait(timeout)


@dataclass
class Health:
    """A module's health for the UI status bar."""

    module: str
    ok: bool
    detail: str = ""


@runtime_checkable
class Lifecycle(Protocol):
    """Start, stop and report health (PDF "Module template")."""

    def start(self) -> None:
        """Connect, open devices, subscribe to events."""

    def stop(self) -> None:
        """Close cleanly with no leftover threads."""

    def health(self) -> Health:
        """Current health, shown as a dot in the UI."""
        ...


class SpeechToText(Protocol):
    """WS2 live streaming: audio in -> partial and final text out (via callbacks)."""

    def run_blocking(self, read_audio: AudioReader, stop: threading.Event) -> None:
        """Stream mic audio until ``stop`` is set (runs on its own thread)."""


class BatchSpeechToText(Protocol):
    """WS2 one-shot transcription for push-to-talk and fallback."""

    def transcribe(self, pcm: bytes, sample_rate: int) -> tuple[str, str | None]:
        """Return ``(text, language_code)`` for 16-bit mono PCM."""
        ...


class Brain(Protocol):
    """WS3: text + language + history -> reply and at most one tool call."""

    def decide(
        self, text: str, language: str | None, cancel: CancelToken | None = None
    ) -> BrainResult:
        """One LLM call. Never raises; failures become a spoken apology."""
        ...

    def summarise(self, result: BrainResult, outcome: ActionResult) -> str:
        """Turn an action outcome into one short spoken sentence in the user's language."""
        ...

    def record_turn(self, user_text: str, reply: str, language: str) -> None:
        """Save the finished turn to conversation memory."""

    def record_action(self, outcome: ActionResult) -> None:
        """Remember an action outcome, so "isko" / "it" can refer to it next turn."""


class ActionRunner(Protocol):
    """WS4: list tools for the LLM, run one approved tool call."""

    def tool_schemas(self) -> list[dict[str, Any]]:
        """OpenAI-style tool definitions built from the registry."""
        ...

    def run(self, call: ToolCall, timeout_s: float = 5.0) -> ActionResult:
        """Run one tool call off the caller's thread with a timeout. Never raises."""
        ...


class Guard(Protocol):
    """WS5: allow / confirm / deny, and judge yes answers."""

    def check(self, call: ToolCall, language: str | None = None,
              romanised: bool = False) -> SafetyDecision:
        """Allow-list, argument validation, rate limit, risk level."""
        ...

    def is_yes(self, text: str | None) -> bool:
        """True only for a clear yes ("haan", "yes", "aam", "avunu", ...)."""
        ...


class TextToSpeech(Protocol):
    """WS6: text in a language -> a stream of 16-bit mono PCM chunks."""

    sample_rate: int

    def stream(self, text: str, language: str | None) -> Iterator[bytes]:
        """Yield PCM chunks; stop early when the iterator is closed (cancel)."""
        ...

"""Every event that travels on the event bus (PDF section "Shared contracts").

Each event carries a ``turn_id`` (one per user sentence, used to trace a turn
across modules) and a ``ts`` wall-clock timestamp (used for the speed readout).
Both are keyword-only, so subclasses keep their own positional fields.

This file is a frozen contract: changing it needs the tech lead's review.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

_turn_counter = itertools.count(1)


def new_turn_id() -> str:
    """A short, unique, sortable id for one user sentence, e.g. ``t0007-1730960000``."""
    return f"t{next(_turn_counter):04d}-{int(time.time())}"


@dataclass(kw_only=True)
class Event:
    """Base class: every event has a turn id and a timestamp."""

    turn_id: str = ""
    ts: float = field(default_factory=time.time)

    @property
    def kind(self) -> str:
        """Short snake_case name of the event type, e.g. ``final_transcript``."""
        name = type(self).__name__
        return "".join(f"_{c.lower()}" if c.isupper() else c for c in name).lstrip("_")


# -- audio and speech-to-text ------------------------------------------------------


@dataclass
class AudioChunk(Event):
    """100 ms of 16 kHz mono 16-bit mic audio. Never sent while the assistant speaks."""

    pcm: bytes


@dataclass
class MicLevel(Event):
    """Mic loudness 0.0-1.0 for the UI level meter."""

    level: float


@dataclass
class PartialTranscript(Event):
    """Text recognised so far. For the live on-screen transcript only."""

    text: str


@dataclass
class FinalTranscript(Event):
    """The full sentence, sent when the user pauses. Starts a turn."""

    text: str
    language: str | None = None
    confidence: float | None = None


# -- brain, safety, actions -----------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    """One action the LLM asked for: a name from the allow-list plus its arguments."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""


@dataclass
class BrainResult(Event):
    """The brain's decision: a spoken reply and at most one tool call."""

    reply: str
    language: str
    romanised: bool = False
    tool_call: ToolCall | None = None
    llm_ms: int = 0


class Verdict(StrEnum):
    """What the safety guard decided for a tool call."""

    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass
class SafetyDecision(Event):
    """Allow, ask first, or deny a tool call. ``question`` is spoken when confirming."""

    verdict: Verdict
    tool_call: ToolCall
    reason: str = ""
    question: str = ""
    risk: str = "low"
    clean_args: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionResult(Event):
    """Outcome of one tool call.

    ``status`` is ``ok`` | ``error`` | ``denied`` | ``blocked`` | ``cancelled`` | ``timeout``.
    ``fact`` is a short English fact (e.g. "volume 40") used to phrase the reply.
    """

    name: str
    args: dict[str, Any]
    status: str
    result: Any
    duration_ms: int = 0
    informational: bool = False
    extra: dict[str, Any] = field(default_factory=dict)
    fact: str = ""

    @property
    def ok(self) -> bool:
        """True when the action ran successfully."""
        return self.status == "ok"


# -- text-to-speech -----------------------------------------------------------------


@dataclass
class SpeakRequest(Event):
    """Text for text-to-speech, in a language."""

    text: str
    language: str | None = None


@dataclass
class SpeakStarted(Event):
    """First audio of a reply is playing. Mutes the mic."""


@dataclass
class SpeakFinished(Event):
    """Playback ended (or was interrupted). Unmutes the mic after a short delay."""

    interrupted: bool = False


@dataclass
class Interrupt(Event):
    """Stop button, F9 or a spoken "ruko": everyone stops immediately."""

    reason: str = "user"


# -- orchestration, metrics, status ---------------------------------------------------


class TurnState(StrEnum):
    """The orchestrator's states (PDF section "Orchestrator, states")."""

    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    CHECKING = "checking"
    AWAITING_YES = "awaiting_yes"
    ACTING = "acting"
    SPEAKING = "speaking"


@dataclass
class StateChanged(Event):
    """The orchestrator moved from one state to another."""

    old: TurnState
    new: TurnState


@dataclass
class Metrics(Event):
    """Timings for one turn in milliseconds. ``total_ms`` = end of speech to first sound."""

    stt_ms: int = 0
    llm_ms: int = 0
    action_ms: int = 0
    tts_first_ms: int = 0
    total_ms: int = 0


@dataclass
class Status(Event):
    """A short human-readable status line for the UI."""

    text: str


@dataclass
class ErrorRaised(Event):
    """A problem to show the user. Never a crash."""

    message: str
    module: str = ""


@dataclass
class LanguageDetected(Event):
    """The language code of the current turn, e.g. ``hi-IN``."""

    language: str


@dataclass
class HealthChanged(Event):
    """A module's health for the UI status bar dots."""

    module: str
    ok: bool
    detail: str = ""


@dataclass
class ModeChanged(Event):
    """UI control changed: live mode, wake word, language hint."""

    live: bool | None = None
    language_hint: str | None = None

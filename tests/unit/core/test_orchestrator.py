"""Orchestrator tests with small inline stubs (no audio devices, no Sarvam, no PC actions).

They follow the PDF state table: Thinking -> Checking -> (Awaiting yes) -> Acting ->
Speaking -> Listening, one tool call per turn, interrupt from any state, errors
become a spoken apology.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Iterator
from typing import Any

import pytest

from core.bus import EventBus
from core.config import Settings, load_settings
from core.events import (
    ActionResult,
    BrainResult,
    ErrorRaised,
    Event,
    HealthChanged,
    Interrupt,
    Metrics,
    SafetyDecision,
    SpeakFinished,
    SpeakRequest,
    StateChanged,
    ToolCall,
    TurnState,
    Verdict,
)
from core.interfaces import CancelToken, Health
from core.orchestrator import Orchestrator

# -- stubs ------------------------------------------------------------------------------


class StubBrain:
    def __init__(self, decide: Callable[[str], BrainResult] | BrainResult) -> None:
        self._decide = decide
        self.turns: list[tuple[str, str]] = []
        self.actions: list[ActionResult] = []
        self.delay = 0.0

    def decide(self, text: str, language: str | None, cancel: CancelToken | None = None
               ) -> BrainResult:
        if self.delay:
            time.sleep(self.delay)
        result = self._decide(text) if callable(self._decide) else self._decide
        return BrainResult(result.reply, result.language, result.romanised, result.tool_call,
                           llm_ms=5)

    def summarise(self, result: BrainResult, outcome: ActionResult) -> str:
        return f"summary {outcome.name} {outcome.status}"

    def record_turn(self, user_text: str, reply: str, language: str) -> None:
        self.turns.append((user_text, reply))

    def record_action(self, outcome: ActionResult) -> None:
        self.actions.append(outcome)

    def health(self) -> Health:
        return Health("brain", True)


class StubGuard:
    def __init__(self, verdict: Verdict = Verdict.ALLOW, reason: str = "") -> None:
        self.verdict = verdict
        self.reason = reason

    def check(self, call: ToolCall, language: str | None = None, romanised: bool = False
              ) -> SafetyDecision:
        return SafetyDecision(self.verdict, call, self.reason, question="pakka?",
                              clean_args=dict(call.args))

    def is_yes(self, text: str | None) -> bool:
        return (text or "").strip().lower() in ("haan", "yes")


class StubRunner:
    def __init__(self, delay: float = 0.0, status: str = "ok") -> None:
        self.calls: list[ToolCall] = []
        self.delay = delay
        self.status = status

    def tool_schemas(self) -> list[dict[str, Any]]:
        return []

    def run(self, call: ToolCall, timeout_s: float = 5.0) -> ActionResult:
        self.calls.append(call)
        if self.delay:
            time.sleep(self.delay)
        return ActionResult(call.name, dict(call.args), self.status, "done", duration_ms=7,
                            fact="done")


class StubTTS:
    sample_rate = 24000

    def stream(self, text: str, language: str | None) -> Iterator[bytes]:
        for _ in range(3):
            yield b"\x00\x00" * 240


class StubPlayer:
    def __init__(self, chunk_s: float = 0.01) -> None:
        self.spoken = 0
        self.chunk_s = chunk_s
        self._stop = threading.Event()
        self._speaking = False

    def play(self, chunks: Iterable[bytes], on_first_audio: Callable[[], None] | None = None
             ) -> None:
        self._stop.clear()
        self._speaking = True
        first = True
        try:
            for _ in chunks:
                if self._stop.is_set():
                    return
                if first and on_first_audio:
                    on_first_audio()
                first = False
                time.sleep(self.chunk_s)
            self.spoken += 1
        finally:
            self._speaking = False

    def stop(self) -> None:
        self._stop.set()

    @property
    def is_speaking(self) -> bool:
        return self._speaking


class StubMic:
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def read(self, timeout: float) -> bytes | None:
        time.sleep(min(timeout, 0.01))
        return None

    def drain(self) -> None: ...


class StubSTT:
    """Live STT that emits scripted events, then waits for stop."""

    def __init__(self, on_partial, on_final, on_event, script: list[tuple[str, Any]]) -> None:
        self.on_partial, self.on_final, self.on_event = on_partial, on_final, on_event
        self.script = script
        self.language_hint = ""

    def run_blocking(self, read_audio, stop: threading.Event) -> None:
        for kind, value in self.script:
            if kind == "event":
                self.on_event(value)
            elif kind == "final":
                self.on_final(*value)
                time.sleep(0.3)
        stop.wait(5)


# -- helpers ---------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path) -> Settings:
    return load_settings(env_file=None, settings_file=None, overrides={
        "SARVAM_API_KEY": "sk_test", "CONFIRM_TIMEOUT_S": "1", "FILLER_AFTER_MS": "0",
        "ACTION_TIMEOUT_S": "1", "DB_PATH": ":memory:", "LOG_DIR": str(tmp_path),
    })


class Harness:
    def __init__(self, settings: Settings, brain: StubBrain, guard: StubGuard | None = None,
                 runner: StubRunner | None = None, script: list | None = None,
                 wake_filter=None) -> None:
        self.bus = EventBus()
        self.events: list[Event] = []
        self._lock = threading.Lock()
        self.bus.subscribe((), self._collect, name="test")
        self.brain, self.guard = brain, guard or StubGuard()
        self.runner, self.player = runner or StubRunner(), StubPlayer()
        self.orch = Orchestrator(
            settings, self.bus, self.brain, self.guard, self.runner, StubTTS(), self.player,
            StubMic(),
            lambda p, f, e: StubSTT(p, f, e, script or []),
            wake_filter=wake_filter,
            modules={"brain": brain},
        )

    def _collect(self, event: Event) -> None:
        with self._lock:
            self.events.append(event)

    def of(self, kind: type) -> list[Any]:
        with self._lock:
            return [e for e in self.events if isinstance(e, kind)]

    def wait(self, check: Callable[[], bool], timeout: float = 5.0) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if check():
                return
            time.sleep(0.01)
        raise AssertionError("timed out; events: " + ", ".join(e.kind for e in self.events))

    def wait_idle(self) -> None:
        time.sleep(0.05)
        self.wait(lambda: not self.orch.busy and bool(self.of(Metrics)))
        time.sleep(0.05)  # let the bus deliver the last events

    def close(self) -> None:
        self.orch.shutdown()
        self.bus.close()


def reply(text: str, tool: ToolCall | None = None) -> BrainResult:
    return BrainResult(text, "hi-IN", True, tool)


@pytest.fixture
def make(settings: Settings):
    created: list[Harness] = []

    def factory(*args: Any, **kwargs: Any) -> Harness:
        h = Harness(settings, *args, **kwargs)
        created.append(h)
        return h

    yield factory
    for h in created:
        h.close()


# -- tests ------------------------------------------------------------------------------


def test_reply_only_turn_speaks_and_records(make) -> None:
    h = make(StubBrain(reply("Delhi Bharat ki rajdhani hai.")))
    h.orch.submit_typed("Bharat ki rajdhani kya hai?")
    h.wait_idle()
    spoken = [e.text for e in h.of(SpeakRequest)]
    assert spoken == ["Delhi Bharat ki rajdhani hai."]
    assert h.brain.turns == [("Bharat ki rajdhani kya hai?", "Delhi Bharat ki rajdhani hai.")]
    states = [e.new for e in h.of(StateChanged)]
    assert TurnState.THINKING in states and TurnState.SPEAKING in states
    assert not h.runner.calls


def test_every_event_of_a_turn_has_the_same_turn_id(make) -> None:
    h = make(StubBrain(reply("ok", ToolCall("open_app", {"name": "notepad"}))))
    h.orch.submit_typed("Notepad kholo")
    h.wait_idle()
    turn_events = [e for e in h.events if isinstance(
        e, (BrainResult, SafetyDecision, ActionResult, SpeakRequest, Metrics))]
    ids = {e.turn_id for e in turn_events}
    assert len(ids) == 1 and ids.pop().startswith("t")


def test_allowed_tool_runs_once_and_result_is_spoken(make) -> None:
    h = make(StubBrain(reply("Notepad khol raha hoon", ToolCall("open_app", {"name": "notepad"}))))
    h.orch.submit_typed("Notepad kholo")
    h.wait_idle()
    assert [c.name for c in h.runner.calls] == ["open_app"]
    assert h.of(ActionResult)[0].status == "ok"
    assert [e.text for e in h.of(SpeakRequest)] == ["summary open_app ok"]
    assert h.brain.actions and h.brain.actions[0].name == "open_app"
    states = [e.new for e in h.of(StateChanged)]
    assert states.index(TurnState.CHECKING) < states.index(TurnState.ACTING) < states.index(
        TurnState.SPEAKING)


def test_slow_action_speaks_progress_sentence_first(make) -> None:
    h = make(StubBrain(reply("Chrome khol raha hoon", ToolCall("open_app", {"name": "chrome"}))),
             runner=StubRunner(delay=0.6))
    h.orch.submit_typed("Chrome kholo")
    h.wait_idle()
    assert [e.text for e in h.of(SpeakRequest)] == ["Chrome khol raha hoon"]


def test_denied_tool_never_runs(make) -> None:
    h = make(StubBrain(reply("", ToolCall("format_disk", {}))),
             guard=StubGuard(Verdict.DENY, "not on the allow-list"))
    h.orch.submit_typed("disk format karo")
    h.wait_idle()
    assert not h.runner.calls
    outcome = h.of(ActionResult)[0]
    assert outcome.status == "blocked"
    assert "summary format_disk blocked" in [e.text for e in h.of(SpeakRequest)]


def test_high_risk_needs_a_clear_yes(make) -> None:
    h = make(StubBrain(reply("", ToolCall("shutdown_pc", {}))), guard=StubGuard(Verdict.CONFIRM))
    h.orch.submit_typed("PC band karo")
    h.wait(lambda: h.orch.awaiting_answer)
    h.orch.submit_typed("haan")
    h.wait_idle()
    assert [c.name for c in h.runner.calls] == ["shutdown_pc"]
    assert TurnState.AWAITING_YES in [e.new for e in h.of(StateChanged)]


def test_anything_but_yes_cancels(make) -> None:
    h = make(StubBrain(reply("", ToolCall("shutdown_pc", {}))), guard=StubGuard(Verdict.CONFIRM))
    h.orch.submit_typed("PC band karo")
    h.wait(lambda: h.orch.awaiting_answer)
    h.orch.submit_typed("pata nahi")
    h.wait_idle()
    assert not h.runner.calls
    assert h.of(ActionResult)[0].status == "denied"
    assert len(h.of(SpeakRequest)) == 2  # question, then "theek hai, cancel kar diya"


def test_silence_times_out_as_no(make) -> None:
    h = make(StubBrain(reply("", ToolCall("lock_pc", {}))), guard=StubGuard(Verdict.CONFIRM))
    h.orch.submit_typed("lock karo")
    h.wait_idle()
    assert not h.runner.calls
    assert h.of(ActionResult)[0].status == "denied"


def test_interrupt_while_awaiting_yes_cancels_everything(make) -> None:
    h = make(StubBrain(reply("", ToolCall("close_app", {"name": "chrome"}))),
             guard=StubGuard(Verdict.CONFIRM))
    h.orch.submit_typed("chrome band karo")
    h.wait(lambda: h.orch.awaiting_answer)
    h.orch.interrupt("user")
    h.wait_idle()
    assert not h.runner.calls
    assert h.of(Interrupt)


def test_spoken_stop_word_interrupts_speech(make) -> None:
    h = make(StubBrain(reply("Ek lambi kahani " * 5)))
    h.player.chunk_s = 0.3
    h.orch.submit_typed("kahani sunao")
    h.wait(lambda: h.player.is_speaking)
    h.orch._on_final("ruko", "hi-IN")
    h.wait(lambda: bool(h.of(Interrupt)))
    h.wait_idle()
    finished = h.of(SpeakFinished)
    assert finished and finished[-1].interrupted
    assert h.of(Interrupt)[0].reason == "voice"


def test_action_timeout_is_reported(make) -> None:
    h = make(StubBrain(reply("", ToolCall("open_app", {"name": "word"}))),
             runner=StubRunner(delay=2.5))
    h.orch.submit_typed("Word kholo")
    h.wait_idle()
    assert h.of(ActionResult)[0].status == "timeout"


def test_brain_crash_becomes_spoken_apology(make) -> None:
    calls = {"n": 0}

    def flaky(text: str) -> BrainResult:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("LLM down")
        return reply("theek hai")

    h = make(StubBrain(flaky))
    h.orch.submit_typed("kuch bhi")
    h.wait(lambda: not h.orch.busy and bool(h.of(SpeakRequest)))
    assert h.of(ErrorRaised)
    h.orch.submit_typed("phir se")  # the loop is still alive
    h.wait(lambda: len(h.of(SpeakRequest)) >= 2)


def test_filler_is_spoken_when_the_brain_is_slow(settings: Settings) -> None:
    from dataclasses import replace

    brain = StubBrain(reply("Jawab"))
    brain.delay = 0.4
    h = Harness(replace(settings, filler_after_ms=100), brain)
    try:
        h.orch.submit_typed("sawaal")
        h.wait_idle()
        texts = [e.text for e in h.of(SpeakRequest)]
        assert len(texts) == 2 and texts[-1] == "Jawab"
    finally:
        h.close()


def test_wake_word_filter_and_metrics(make) -> None:
    h = make(StubBrain(reply("ji")),
             wake_filter=lambda text, word: text.split(" ", 1)[1] if text.startswith(word) else "",
             script=[("final", ("time batao", "hi-IN")), ("event", "speech_end"),
                     ("final", ("sarvam time batao", "hi-IN"))])
    h.orch.set_wake_word("sarvam")
    assert h.orch.start_live()
    h.wait(lambda: bool(h.of(Metrics)))
    assert [t for t, _ in h.brain.turns] == ["time batao"]
    assert len(h.brain.turns) == 1  # the first sentence had no wake word
    metrics = h.of(Metrics)[0]
    assert metrics.total_ms >= metrics.tts_first_ms >= 0
    h.orch.stop_live()


def test_stt_fallback_reports_health_and_error(make) -> None:
    h = make(StubBrain(reply("ok")), script=[("event", "fallback")])
    assert h.orch.start_live()
    h.wait(lambda: any(isinstance(e, HealthChanged) and e.module == "stt" and not e.ok
                       for e in h.events))
    h.wait(lambda: any(e.module == "stt" for e in h.of(ErrorRaised)))
    h.orch.stop_live()


def test_health_reports_registered_modules(make) -> None:
    h = make(StubBrain(reply("ok")))
    assert [x.module for x in h.orch.health()] == ["brain"]

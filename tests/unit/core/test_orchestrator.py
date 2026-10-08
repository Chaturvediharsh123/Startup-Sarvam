"""Tests for the real-time pipeline with fake STT, brain, TTS and audio devices."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from audio.mic import MuteFlag
from audio.speaker import AudioPlayer
from brain.brain import Reply
from core.config import Settings, load_settings
from core.orchestrator import Pipeline, PipelineEvent
from safety.guard import ActionResult


class FakeMic:
    def __init__(self) -> None:
        self.chunks: queue.Queue[bytes] = queue.Queue()
        self.running = False
        self.starts = 0

    def start(self) -> None:
        self.running = True
        self.starts += 1

    def stop(self) -> None:
        self.running = False

    def drain(self) -> None:
        while not self.chunks.empty():
            self.chunks.get_nowait()

    def read(self, timeout: float = 0.5) -> bytes | None:
        try:
            return self.chunks.get(timeout=timeout)
        except queue.Empty:
            return None


class FakeSTT:
    """Live STT stand-in: tests call the callbacks directly."""

    instances: list[FakeSTT] = []

    def __init__(self, on_partial: Callable, on_final: Callable, on_event: Callable) -> None:
        self.on_partial, self.on_final, self.on_event = on_partial, on_final, on_event
        self.started = threading.Event()
        self.closed = threading.Event()
        FakeSTT.instances.append(self)

    def run_blocking(self, read_audio: Callable, stop: threading.Event) -> None:
        self.started.set()
        while not stop.is_set():
            read_audio(0.05)
        self.closed.set()


class FakeBrain:
    def __init__(self, ask_question: str | None = None, fail_first: bool = False) -> None:
        self.ask_question = ask_question
        self.fail_first = fail_first
        self.calls: list[tuple[str, str | None]] = []
        self.answers: list[str] = []

    def handle(self, text: str, language: str | None = None, ask_user: Any = None) -> Reply:
        self.calls.append((text, language))
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError("brain exploded")
        actions = []
        if self.ask_question:
            answer = ask_user(self.ask_question)
            self.answers.append(answer)
            status = "ok" if answer == "haan" else "denied"
            actions.append(ActionResult("shutdown_pc", {}, status, "x"))
        return Reply(f"reply to {text}", "hi-IN", actions=actions,
                     timings={"llm_ms": 5, "total_ms": 7})


class FakeTTS:
    def __init__(self, mute: MuteFlag) -> None:
        self.mute = mute
        self.spoken: list[str] = []
        self.muted_during: list[bool] = []

    def stream(self, text: str, language: str | None) -> Iterator[bytes]:
        self.spoken.append(text)
        yield b"\x00\x00" * 10
        self.muted_during.append(self.mute.is_muted)
        yield b"\x00\x00" * 10


class NullStream:
    def start(self) -> None: ...
    def write(self, data: bytes) -> None: ...
    def stop(self) -> None: ...
    def abort(self) -> None: ...
    def close(self) -> None: ...


@pytest.fixture
def quick_settings(tmp_path: Any) -> Settings:
    return load_settings(env_file=None, overrides={
        "SARVAM_API_KEY": "sk_test", "DB_PATH": ":memory:", "LOG_DIR": str(tmp_path),
        "CONFIRM_TIMEOUT_S": "1",
    })


def make_pipeline(
    settings: Settings, brain: FakeBrain
) -> tuple[Pipeline, FakeTTS, MuteFlag, FakeMic]:
    FakeSTT.instances.clear()
    mute = MuteFlag()
    tts = FakeTTS(mute)
    player = AudioPlayer(24000, mute, unmute_delay_s=0.0, stream_factory=lambda sr: NullStream())
    mic = FakeMic()
    pipe = Pipeline(settings, brain, tts, player, mic, FakeSTT)  # type: ignore[arg-type]
    return pipe, tts, mute, mic


def wait_for(pipe: Pipeline, kind: str, timeout: float = 3.0) -> list[PipelineEvent]:
    """Collect events until one of ``kind`` arrives."""
    seen: list[PipelineEvent] = []
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            event = pipe.events.get(timeout=0.05)
        except queue.Empty:
            continue
        seen.append(event)
        if event.kind == kind:
            return seen
    raise AssertionError(f"no {kind!r} event; got {[e.kind for e in seen]}")


def test_final_transcript_produces_reply(quick_settings: Settings) -> None:
    brain = FakeBrain()
    pipe, tts, mute, mic = make_pipeline(quick_settings, brain)
    try:
        assert pipe.start_live()
        stt = FakeSTT.instances[0]
        assert stt.started.wait(1)
        stt.on_event("speech_end")
        stt.on_partial("Notepad")
        stt.on_final("Notepad kholo", "hi-IN")
        events = wait_for(pipe, "timing")
        kinds = [e.kind for e in events]
        assert kinds.index("partial") < kinds.index("final") < kinds.index("reply")
        assert brain.calls == [("Notepad kholo", "hi-IN")]
        assert tts.spoken == ["reply to Notepad kholo"]
        timing = events[-1].data
        assert {"stt_ms", "llm_ms", "tts_first_ms", "total_ms"} <= set(timing)
    finally:
        pipe.shutdown()


def test_mic_muted_during_speech(quick_settings: Settings) -> None:
    pipe, tts, mute, mic = make_pipeline(quick_settings, FakeBrain())
    try:
        pipe.submit("Battery kitni hai?")
        wait_for(pipe, "timing")
        assert tts.muted_during == [True]
        assert not mute.is_muted
    finally:
        pipe.shutdown()


def test_stop_live_closes_connection(quick_settings: Settings) -> None:
    pipe, _, _, mic = make_pipeline(quick_settings, FakeBrain())
    try:
        pipe.start_live()
        stt = FakeSTT.instances[0]
        assert stt.started.wait(1)
        assert mic.running
        pipe.stop_live()
        assert stt.closed.is_set()
        assert not pipe.live
        assert not mic.running
    finally:
        pipe.shutdown()


def test_exception_in_turn_does_not_kill_loop(quick_settings: Settings) -> None:
    brain = FakeBrain(fail_first=True)
    pipe, tts, _, _ = make_pipeline(quick_settings, brain)
    try:
        pipe.submit("first")
        wait_for(pipe, "error")
        pipe.submit("second")
        wait_for(pipe, "timing")
        assert tts.spoken == ["reply to second"]
    finally:
        pipe.shutdown()


def test_voice_confirmation_routes_next_final(quick_settings: Settings) -> None:
    brain = FakeBrain(ask_question="PC band karoon?")
    pipe, tts, _, _ = make_pipeline(quick_settings, brain)
    try:
        pipe.start_live()
        stt = FakeSTT.instances[0]
        stt.on_final("PC band karo", "hi-IN")
        wait_for(pipe, "status")  # thinking
        deadline = time.monotonic() + 2
        while not pipe._confirm_pending.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        stt.on_final("haan", "hi-IN")
        events = wait_for(pipe, "timing")
        assert brain.answers == ["haan"]
        assert brain.calls == [("PC band karo", "hi-IN")]  # "haan" is not a new turn
        assert tts.spoken[0] == "PC band karoon?"
        assert any(e.kind == "action" and e.data.status == "ok" for e in events)
    finally:
        pipe.shutdown()


def test_confirmation_timeout_counts_as_no(quick_settings: Settings) -> None:
    brain = FakeBrain(ask_question="PC band karoon?")
    pipe, _, _, _ = make_pipeline(quick_settings, brain)
    try:
        pipe.submit("PC band karo")
        events = wait_for(pipe, "timing", timeout=5)
        assert brain.answers == [""]
        assert any(e.kind == "action" and e.data.status == "denied" for e in events)
    finally:
        pipe.shutdown()


def test_busy_ignores_new_speech(quick_settings: Settings) -> None:
    gate = threading.Event()

    class SlowBrain(FakeBrain):
        def handle(self, text: str, language: str | None = None, ask_user: Any = None) -> Reply:
            gate.wait(2)
            return super().handle(text, language, ask_user)

    brain = SlowBrain()
    pipe, _, _, _ = make_pipeline(quick_settings, brain)
    try:
        pipe.start_live()
        stt = FakeSTT.instances[0]
        stt.on_final("first", None)
        stt.on_final("second", None)
        gate.set()
        wait_for(pipe, "timing")
        assert [c[0] for c in brain.calls] == ["first"]
    finally:
        pipe.shutdown()


def test_wake_word_filter(tmp_path: Any) -> None:
    settings = load_settings(env_file=None, overrides={
        "SARVAM_API_KEY": "sk_test", "DB_PATH": ":memory:", "LOG_DIR": str(tmp_path),
        "WAKE_WORD": "sarvam",
    })
    brain = FakeBrain()
    pipe, _, _, _ = make_pipeline(settings, brain)
    try:
        pipe.start_live()
        stt = FakeSTT.instances[0]
        stt.on_final("notepad kholo", None)
        stt.on_final("Sarvam, notepad kholo", None)
        wait_for(pipe, "timing")
        assert brain.calls == [("notepad kholo", None)]
    finally:
        pipe.shutdown()


def test_barge_in_stops_speech(tmp_path: Any) -> None:
    settings = load_settings(env_file=None, overrides={
        "SARVAM_API_KEY": "sk_test", "DB_PATH": ":memory:", "LOG_DIR": str(tmp_path),
        "BARGE_IN": "true",
    })
    pipe, _, _, _ = make_pipeline(settings, FakeBrain())
    stopped: list[bool] = []
    pipe.player.stop = lambda: stopped.append(True)  # type: ignore[method-assign]
    pipe.player._speaking.set()
    try:
        pipe._on_stt_event("speech_start")
        assert stopped == [True]
    finally:
        pipe.player._speaking.clear()
        pipe.shutdown()


def test_push_to_talk(quick_settings: Settings) -> None:
    brain = FakeBrain()
    pipe, _, _, mic = make_pipeline(quick_settings, brain)

    class FakeRest:
        def transcribe(self, pcm: bytes, rate: int) -> tuple[str, str | None]:
            assert pcm == b"\x01\x00" * 1600
            return "Chrome kholo", "hi-IN"

    pipe.rest_stt = FakeRest()
    try:
        pipe.ptt_press()
        assert mic.running
        mic.chunks.put(b"\x01\x00" * 1600)
        time.sleep(0.3)
        pipe.ptt_release()
        assert not mic.running
        wait_for(pipe, "timing")
        assert brain.calls == [("Chrome kholo", "hi-IN")]
    finally:
        pipe.shutdown()


def test_shutdown_joins_threads(quick_settings: Settings) -> None:
    pipe, _, _, _ = make_pipeline(quick_settings, FakeBrain())
    pipe.start_live()
    pipe.shutdown()
    assert not pipe._worker.is_alive()
    assert not pipe.live

"""Tests for UI helpers and a hidden-window smoke test of event handling."""

from __future__ import annotations

import queue
from pathlib import Path
from typing import Any

import pytest

from core.config import Settings, save_env_values
from core.orchestrator import PipelineEvent
from safety.guard import ActionResult
from ui.app import format_action, format_timing


def test_format_timing() -> None:
    text = format_timing(
        {"stt_ms": 400, "llm_ms": 900, "action_ms": 0, "tts_first_ms": 300, "total_ms": 1600}
    )
    assert text == "STT 0.4s · LLM 0.9s · voice 0.3s · total 1.6s"


def test_format_action() -> None:
    ok = ActionResult("open_app", {"name": "notepad"}, "ok", "Opened notepad")
    denied = ActionResult("shutdown_pc", {}, "denied", "user did not confirm")
    info = ActionResult("system_status", {}, "ok", {"battery_percent": 80})
    assert format_action(ok) == "✓  open_app(name=notepad) — Opened notepad"
    assert format_action(denied).startswith("✗ denied")
    assert format_action(info).endswith("— info")


def test_save_env_values_keeps_other_lines(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# comment\nSARVAM_API_KEY=sk_secret\nTTS_SPEAKER=shubh\n", encoding="utf-8")
    save_env_values({"TTS_SPEAKER": "priya", "WAKE_WORD": "sarvam"}, env)
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "# comment", "SARVAM_API_KEY=sk_secret", "TTS_SPEAKER=priya", "WAKE_WORD=sarvam"
    ]


class FakePipeline:
    def __init__(self) -> None:
        self.events: queue.Queue[PipelineEvent] = queue.Queue()
        self.live_calls: list[str] = []
        self.shut = False
        self.settings: Any = None
        self.tts = type("T", (), {"settings": None})()

    def start_live(self) -> bool:
        self.live_calls.append("start")
        return True

    def stop_live(self) -> None:
        self.live_calls.append("stop")

    def stop_speaking(self) -> None: ...
    def ptt_press(self) -> None: ...
    def ptt_release(self) -> None: ...

    def shutdown(self) -> None:
        self.shut = True


def test_window_handles_events(settings: Settings) -> None:
    import tkinter

    from ui.app import AssistantApp

    pipe = FakePipeline()
    try:
        app = AssistantApp(pipe, settings, start_live=True)  # type: ignore[arg-type]
    except tkinter.TclError as exc:  # no display available
        pytest.skip(f"no display: {exc}")
    try:
        app.withdraw()
        assert pipe.live_calls == ["start"]
        app.handle_event(PipelineEvent("partial", "नोटपैड"))
        app.handle_event(PipelineEvent("final", "नोटपैड खोलो"))
        app.handle_event(PipelineEvent("reply", "नोटपैड खोल दिया।"))
        app.handle_event(PipelineEvent("action", ActionResult("open_app", {"name": "notepad"},
                                                              "ok", "Opened notepad")))
        app.handle_event(PipelineEvent("timing", {"llm_ms": 900, "total_ms": 1200}))
        app.handle_event(PipelineEvent("language", "hi-IN"))
        transcript = app.transcript.get("1.0", "end")
        assert "नोटपैड खोलो" in transcript
        assert transcript.count("नोटपैड") == 1  # partial was replaced, not duplicated
        assert app.reply.cget("text") == "नोटपैड खोल दिया।"
        assert "open_app" in app.log.get("1.0", "end")
        assert app.speed.cget("text") == "LLM 0.9s · total 1.2s"
        app._apply_settings({"TTS_SPEAKER": "ishita", "WAKE_WORD": "sarvam"})
        assert pipe.settings.tts_speaker == "ishita"
        assert pipe.settings.wake_word == "sarvam"
        app.live_switch.deselect()
        app._toggle_live()
        assert pipe.live_calls == ["start", "stop"]
    finally:
        app.on_close()
    assert pipe.shut

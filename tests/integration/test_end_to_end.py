"""End-to-end tests: the real modules wired by ``main.build_app`` (PDF test level 3).

Everything is real except the edges: the LLM is the keyword-rule ``FakeLLM``, the
voice is ``MockTTS`` with a recording speaker, and every call that could change
the PC (processes, browser, volume, keys, lock, shutdown) is mocked by the
shared fixtures in ``tests/conftest.py``.
"""

from __future__ import annotations

import json
import sys
import time
import types
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import main
from core.config import Settings
from core.events import (
    ActionResult,
    Event,
    FinalTranscript,
    Metrics,
    SafetyDecision,
    SpeakRequest,
    StateChanged,
    TurnState,
)


@pytest.fixture
def fake_psutil(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """psutil with one running chrome.exe, so close_app has something to close."""
    import psutil

    proc = MagicMock(name="chrome-process")
    proc.info = {"name": "chrome.exe"}
    module = types.ModuleType("psutil")
    module.process_iter = MagicMock(return_value=[proc])  # type: ignore[attr-defined]
    module.wait_procs = MagicMock(return_value=([proc], []))  # type: ignore[attr-defined]
    module.NoSuchProcess = psutil.NoSuchProcess  # type: ignore[attr-defined]
    for name in ("sensors_battery", "disk_usage", "cpu_percent", "virtual_memory"):
        setattr(module, name, getattr(psutil, name))
    monkeypatch.setitem(sys.modules, "psutil", module)
    return proc


@pytest.fixture
def app(settings: Settings, tmp_path: Path, no_side_effects, fake_volume, fake_pyautogui,
        fake_pyperclip) -> Iterator[Any]:
    settings = replace(settings, db_path=tmp_path / "assistant.db", filler_after_ms=0,
                       confirm_timeout_s=2.0)
    application = main.build_app(settings, mock=True, text=True, kill_switch=False)
    events: list[Event] = []
    application.bus.subscribe((), events.append, name="test")
    application.events = events  # type: ignore[attr-defined]
    application.mocks = no_side_effects  # type: ignore[attr-defined]
    yield application
    application.close()


def say(app: Any, text: str, answer: str | None = None) -> list[Event]:
    """Type one command (and a yes/no answer if asked); return that turn's events."""
    start = len(app.events)
    orchestrator = app.orchestrator
    orchestrator.submit_typed(text)
    deadline = time.monotonic() + 10
    asked = False
    while time.monotonic() < deadline:
        if orchestrator.awaiting_answer and answer is not None and not asked:
            asked = True
            orchestrator.submit_typed(answer)
        if not orchestrator.busy and any(isinstance(e, Metrics) for e in app.events[start:]):
            break
        time.sleep(0.02)
    time.sleep(0.1)
    return app.events[start:]


def of(events: list[Event], kind: type) -> list[Any]:
    return [e for e in events if isinstance(e, kind)]


def test_open_app_runs_through_every_layer(app: Any) -> None:
    events = say(app, "Notepad kholo")
    outcome = of(events, ActionResult)[0]
    assert outcome.name == "open_app" and outcome.ok
    app.mocks["startfile"].assert_called_once()
    states = [e.new for e in of(events, StateChanged)]
    for state in (TurnState.THINKING, TurnState.CHECKING, TurnState.ACTING, TurnState.SPEAKING):
        assert state in states
    assert of(events, SpeakRequest), "the result must be spoken"
    turn_ids = {e.turn_id for e in events if isinstance(
        e, (FinalTranscript, SafetyDecision, ActionResult, SpeakRequest, Metrics))}
    assert len(turn_ids) == 1


def test_isko_follow_up_uses_memory_and_needs_a_yes(app: Any, fake_psutil: MagicMock) -> None:
    say(app, "Chrome kholo")
    events = say(app, "ab isko band karo", answer="haan")
    decision = of(events, SafetyDecision)[0]
    assert decision.tool_call.name == "close_app"
    assert decision.verdict.value == "confirm"
    outcome = of(events, ActionResult)[0]
    assert outcome.ok and outcome.args.get("name") == "chrome"
    fake_psutil.terminate.assert_called_once()


def test_shutdown_refused_without_yes_never_runs(app: Any) -> None:
    events = say(app, "PC band karo", answer="nahi")
    assert of(events, ActionResult)[0].status == "denied"
    app.mocks["run"].assert_not_called()
    spoken = [e.text for e in of(events, SpeakRequest)]
    assert len(spoken) == 2  # the question, then "theek hai, cancel kar diya"


def test_volume_and_status_and_time(app: Any, fake_volume: MagicMock) -> None:
    say(app, "volume 40 kar do")
    fake_volume.SetMasterVolumeLevelScalar.assert_called_with(0.4, None)
    events = say(app, "time batao")
    assert of(events, ActionResult)[0].name == "current_time"
    assert any(":" in e.text for e in of(events, SpeakRequest))


def test_audit_log_has_every_step_with_turn_id(app: Any) -> None:
    say(app, "Notepad kholo")
    say(app, "PC band karo", answer="nahi")
    path = Path(app.settings.log_dir) / "audit.jsonl"
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert lines, "audit.jsonl must not be empty"
    assert all(line.get("turn_id") for line in lines)
    assert "sk_test_key" not in path.read_text(encoding="utf-8")
    assert app.audit.recent(5)


def test_note_is_saved_in_the_notes_folder(app: Any) -> None:
    events = say(app, "chhutti ke liye application likho")
    outcome = of(events, ActionResult)[0]
    assert outcome.name == "save_note" and outcome.ok
    notes = list(Path(app.settings.notes_dir).glob("*.txt"))
    assert len(notes) == 1 and notes[0].read_text(encoding="utf-8").strip()


def test_health_is_reported_for_every_module(app: Any) -> None:
    names = {h.module for h in app.orchestrator.health()}
    assert {"audio", "brain", "actions", "safety", "tts"} <= names


def test_text_mode_main_runs_a_scripted_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                no_side_effects, capsys) -> None:
    for key, value in {"DB_PATH": str(tmp_path / "a.db"), "LOG_DIR": str(tmp_path / "logs"),
                       "NOTES_DIR": str(tmp_path / "notes"), "FILLER_AFTER_MS": "0"}.items():
        monkeypatch.setenv(key, value)
    lines = iter(["time batao", "PC band karo", "nahi", "exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))
    assert main.main(["--mock", "--text", "--no-kill-switch"]) == 0
    out = capsys.readouterr().out
    assert "current_time" in out and "shutdown_pc" in out and "denied" in out
    no_side_effects["run"].assert_not_called()


def test_window_drives_the_real_orchestrator(app: Any) -> None:
    """Type in the real window -> real orchestrator/brain/safety/actions -> window shows it."""
    tkinter = pytest.importorskip("tkinter")
    pytest.importorskip("customtkinter")
    from ui.app import AssistantApp

    window = None
    for _ in range(5):  # Tcl start-up sometimes fails while pytest captures output
        try:
            window = AssistantApp(app.orchestrator, app.bus, app.settings, None, app.audit)
            break
        except tkinter.TclError:
            continue
    if window is None:
        pytest.skip("no display for Tk")
    window.withdraw()
    try:
        window.entry.insert(0, "time batao")
        window._send_typed()
        end = time.monotonic() + 10
        while time.monotonic() < end:
            window.update()
            if ":" in window.reply.said.cget("text") and not app.orchestrator.busy:
                break
            time.sleep(0.02)
        assert ":" in window.reply.said.cget("text")
        window.update()
        end = time.monotonic() + 3
        while "current_time" not in window.activity.text() and time.monotonic() < end:
            window.update()
            time.sleep(0.02)
        assert "current_time" in window.activity.text()
    finally:
        window.on_close()

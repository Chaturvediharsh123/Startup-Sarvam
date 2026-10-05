"""Tests for the safety gate. Risky functions are replaced so they can never run."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

import assistant.actions as actions
import assistant.safety as safety
from assistant.actions import ACTIONS
from assistant.memory import Memory
from assistant.safety import RateLimiter, execute, is_yes, validate_args


@pytest.fixture
def mem() -> Iterator[Memory]:
    with Memory(":memory:") as m:
        yield m


@pytest.fixture(autouse=True)
def _generous_limit() -> Iterator[None]:
    safety.configure(1000)
    yield
    safety.configure(1000)


@pytest.fixture
def fake_action(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Swap shutdown_pc's function for a mock; the real one must never run."""
    mock = MagicMock(name="shutdown_pc", return_value="Shutdown in 60 seconds")
    monkeypatch.setitem(ACTIONS, "shutdown_pc", replace(ACTIONS["shutdown_pc"], func=mock))
    return mock


@pytest.mark.parametrize(
    "answer",
    ["haan", "Haan ji", "हाँ", "हां", "yes", "Yes please", "ok", "சரி", "ஆம்", "అవును", "ಹೌದು",
     "হ্যাঁ", "होय", "theek hai", "haan kar do", "bilkul", "ठीक है", "sure, go ahead"],
)
def test_is_yes_accepts_clear_yes(answer: str) -> None:
    assert is_yes(answer) is True


@pytest.mark.parametrize(
    "answer",
    [None, "", "   ", "nahi", "नहीं", "haan nahi", "no", "nope", "mat karo", "haan?", "kya?",
     "yes no", "இல்லை", "వద్దు", "ಬೇಡ", "না", "नाही", "hmm", "uh", "wait", "kya bola",
     "haan haan haan but actually let us think about it more", "ok... ruko", "don't"],
)
def test_is_yes_rejects_everything_else(answer: str | None) -> None:
    assert is_yes(answer) is False


def test_unknown_action_blocked_and_logged(mem: Memory, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="actions"):
        outcome = execute("format_disk", {"drive": "C"}, mem, ask_user=None)
    assert outcome.status == "blocked"
    assert "format_disk" in caplog.text
    row = mem._query("SELECT name, status FROM actions")[0]
    assert (row["name"], row["status"]) == ("format_disk", "blocked")


@pytest.mark.parametrize(
    "name, args",
    [
        ("open_app", {"name": "cmd"}),
        ("open_app", {}),
        ("open_app", {"name": "notepad", "extra": 1}),
        ("set_volume", {"level": 150}),
        ("set_volume", {"level": "loud"}),
        ("set_volume", {"level": True}),
        ("press_shortcut", {"shortcut": "alt+f4"}),
        ("type_text", {"text": ""}),
        ("type_text", {"text": "x" * 5000}),
        ("web_search", "not a dict"),
    ],
)
def test_bad_args_rejected(name: str, args: object, mem: Memory) -> None:
    outcome = execute(name, args, mem, ask_user=None)
    assert outcome.status == "error"
    assert "bad arguments" in outcome.result


def test_integer_coercion() -> None:
    spec = ACTIONS["set_volume"]
    assert validate_args(spec, {"level": "30"}) == {"level": 30}
    assert validate_args(spec, {"level": 30.0}) == {"level": 30}


@pytest.mark.parametrize("answer", ["nahi", "", None, "haan nahi", "kya?"])
def test_risky_denied_never_runs(answer: str | None, mem: Memory, fake_action: MagicMock) -> None:
    asked: list[str] = []

    def ask(q: str) -> str | None:
        asked.append(q)
        return answer

    outcome = execute("shutdown_pc", {}, mem, ask, language="hi-IN", romanised=True)
    assert outcome.status == "denied"
    fake_action.assert_not_called()
    assert asked == ["Kya main computer band kar doon? Haan ya nahi boliye."]
    assert mem.last_action() is None


def test_risky_without_ask_user_denied(mem: Memory, fake_action: MagicMock) -> None:
    assert execute("shutdown_pc", {}, mem, ask_user=None).status == "denied"
    fake_action.assert_not_called()


def test_risky_ask_user_crash_denied(mem: Memory, fake_action: MagicMock) -> None:
    def broken(_q: str) -> str:
        raise RuntimeError("mic died")

    assert execute("shutdown_pc", {}, mem, broken).status == "denied"
    fake_action.assert_not_called()


@pytest.mark.parametrize("answer", ["haan", "हाँ", "yes", "சரி"])
def test_risky_confirmed_runs(answer: str, mem: Memory, fake_action: MagicMock) -> None:
    outcome = execute("shutdown_pc", {}, mem, lambda q: answer)
    assert outcome.status == "ok"
    fake_action.assert_called_once_with()
    assert mem.last_action()["name"] == "shutdown_pc"  # type: ignore[index]


def test_confirmation_question_in_user_language(fake_action: MagicMock) -> None:
    asked: list[str] = []
    execute("shutdown_pc", {}, None, lambda q: asked.append(q) or "no", language="ta-IN")
    assert "கணினியை" in asked[0]


def test_close_app_question_names_app(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = MagicMock(return_value="Closed chrome")
    monkeypatch.setitem(ACTIONS, "close_app", replace(ACTIONS["close_app"], func=mock))
    asked: list[str] = []
    execute("close_app", {"name": "chrome"}, None, lambda q: asked.append(q) or "yes", "en-IN")
    assert asked == ["Should I close chrome? Say yes or no."]
    mock.assert_called_once_with(name="chrome")


def test_exception_becomes_error(monkeypatch: pytest.MonkeyPatch, mem: Memory) -> None:
    boom = MagicMock(side_effect=OSError("disk full"))
    monkeypatch.setitem(ACTIONS, "current_time", replace(ACTIONS["current_time"], func=boom))
    outcome = execute("current_time", {}, mem, None)
    assert outcome.status == "error"
    assert "disk full" in outcome.result
    assert mem.last_action() is None


def test_ok_action_timed_and_logged(mem: Memory, no_side_effects: dict[str, MagicMock]) -> None:
    outcome = execute("open_app", {"name": "notepad"}, mem, None)
    assert outcome.ok and outcome.duration_ms >= 0
    assert mem.last_action()["args"] == {"name": "notepad"}  # type: ignore[index]


def test_rate_limit(mem: Memory, no_side_effects: dict[str, MagicMock]) -> None:
    safety.configure(3)
    statuses = [execute("open_app", {"name": "notepad"}, mem, None).status for _ in range(5)]
    assert statuses == ["ok", "ok", "ok", "blocked", "blocked"]
    assert no_side_effects["startfile"].call_count == 3


def test_rate_limiter_window() -> None:
    now = [0.0]
    limiter = RateLimiter(2, clock=lambda: now[0])
    assert limiter.allow() and limiter.allow()
    assert not limiter.allow()
    now[0] = 61.0
    assert limiter.allow()


def test_enable_failsafe(fake_pyautogui: MagicMock) -> None:
    safety.enable_failsafe()
    assert sys.modules["pyautogui"].FAILSAFE is True


def test_real_risky_functions_untouched() -> None:
    # Guard against a test leaking a replaced spec into the registry.
    assert ACTIONS["shutdown_pc"].func is actions.shutdown_pc

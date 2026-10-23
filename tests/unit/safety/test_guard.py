"""The safety guard: verdicts per risk level, argument rules, blocked combos, rate limit."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import actions
from core.config import Settings
from core.events import SafetyDecision, ToolCall, Verdict
from core.interfaces import Guard
from safety.guard import ArgumentError, RateLimiter, SafetyGuard, validate_args


@pytest.fixture
def guard(settings: Settings) -> SafetyGuard:
    return SafetyGuard(settings, schemas_provider=actions.tool_schemas)


def check(guard: SafetyGuard, name: str, args: Any = None, **kw: Any) -> SafetyDecision:
    return guard.check(ToolCall(name, args if args is not None else {}), **kw)


def test_implements_guard_protocol(guard: SafetyGuard) -> None:
    proto: Guard = guard
    assert proto.is_yes("haan") and not proto.is_yes("nahi")
    assert guard.health().ok


@pytest.mark.parametrize(
    "name, args, risk",
    [
        ("open_app", {"name": "notepad"}, "low"),
        ("web_search", {"query": "weather"}, "low"),
        ("youtube_search", {"query": "bhajan"}, "low"),
        ("set_volume", {"level": 40}, "low"),
        ("change_volume", {"direction": "up", "step": 5}, "low"),
        ("scroll", {"direction": "down", "amount": "large"}, "low"),
        ("take_screenshot", {}, "low"),
        ("system_status", {}, "low"),
        ("cancel_shutdown", {}, "low"),
        ("current_time", {}, "low"),
        ("type_text", {"text": "namaste"}, "medium"),
        ("press_shortcut", {"shortcut": "save"}, "medium"),
        ("save_note", {"title": "t", "content": "c"}, "medium"),
    ],
)
def test_low_and_medium_allowed(guard: SafetyGuard, name: str, args: dict, risk: str) -> None:
    decision = check(guard, name, args)
    assert decision.verdict is Verdict.ALLOW
    assert decision.risk == risk
    assert decision.question == ""


def test_medium_is_logged(guard: SafetyGuard, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="actions"):
        check(guard, "type_text", {"text": "hello"})
    assert "medium-risk action allowed: type_text" in caplog.text


@pytest.mark.parametrize(
    "name, args, clean",
    [
        ("close_app", {"name": "crome"}, {"name": "chrome"}),
        ("lock_pc", {}, {}),
        ("shutdown_pc", {}, {}),
    ],
)
def test_high_risk_needs_confirmation(
    guard: SafetyGuard, name: str, args: dict, clean: dict
) -> None:
    decision = check(guard, name, args, language="en-IN")
    assert decision.verdict is Verdict.CONFIRM
    assert decision.risk == "high"
    assert decision.clean_args == clean
    assert decision.question.endswith("Say yes or no.")


def test_confirmation_question_language(guard: SafetyGuard) -> None:
    assert check(guard, "shutdown_pc", language="hi-IN", romanised=True).question == (
        "Kya main computer band kar doon? Haan ya nahi boliye."
    )
    assert "कंप्यूटर" in check(guard, "shutdown_pc", language="hi-IN").question
    assert "கணினியை" in check(guard, "lock_pc", language="ta-IN").question
    assert check(guard, "close_app", {"name": "kroom"}, language="en-IN").question == (
        "Should I close chrome? Say yes or no."
    )


@pytest.mark.parametrize("name", ["format_disk", "run_command", "delete_file", "", "OPEN_APP"])
def test_unknown_tool_denied(guard: SafetyGuard, name: str) -> None:
    decision = check(guard, name, {"x": 1})
    assert decision.verdict is Verdict.DENY
    assert decision.reason == "not on the allow-list"


def test_allowlisted_but_unregistered_denied(settings: Settings, tmp_path: Path) -> None:
    guard = SafetyGuard(settings, schemas_provider=[s for s in actions.tool_schemas()
                                                    if s["function"]["name"] != "lock_pc"])
    assert check(guard, "lock_pc").reason == "not on the allow-list"


@pytest.mark.parametrize(
    "name, args, fragment",
    [
        ("open_app", {"name": "cmd"}, "not an allowed app"),
        ("open_app", {"name": r"C:\Windows\System32\cmd.exe"}, "not an allowed app"),
        ("open_app", {}, "missing arguments"),
        ("open_app", {"name": 5}, "must be an app name"),
        ("open_app", {"name": "notepad", "extra": 1}, "unexpected arguments"),
        ("close_app", {"name": "file manager"}, "cannot be closed"),
        ("set_volume", {"level": 150}, "between 0 and 100"),
        ("set_volume", {"level": "loud"}, "whole number"),
        ("set_volume", {"level": True}, "whole number"),
        ("change_volume", {"direction": "sideways"}, "must be one of"),
        ("change_volume", {"direction": "up", "step": 500}, "between 1 and 50"),
        ("press_shortcut", {"shortcut": "alt+f4"}, "blocked key combination"),
        ("press_shortcut", {"shortcut": "Win + R"}, "blocked key combination"),
        ("press_shortcut", {"shortcut": "ctrl+alt+del"}, "blocked key combination"),
        ("press_shortcut", {"shortcut": "shift+delete"}, "blocked key combination"),
        ("press_shortcut", {"shortcut": "f12"}, "not on the safe shortcut list"),
        ("type_text", {"text": ""}, "non-empty text"),
        ("type_text", {"text": "x" * 5000}, "too long"),
        ("save_note", {"title": "t", "content": "c", "path": "C:\\x"}, "unexpected arguments"),
        ("web_search", "not a dict", "must be an object"),
        ("shutdown_pc", {"delay": 0}, "unexpected arguments"),
    ],
)
def test_bad_arguments_denied(guard: SafetyGuard, name: str, args: Any, fragment: str) -> None:
    decision = check(guard, name, args)
    assert decision.verdict is Verdict.DENY
    assert decision.reason.startswith("bad arguments: ")
    assert fragment in decision.reason
    assert decision.clean_args == {}


@pytest.mark.parametrize(
    "args, clean",
    [
        ({"level": "30"}, {"level": 30}),
        ({"level": 30.0}, {"level": 30}),
    ],
)
def test_integer_coercion(guard: SafetyGuard, args: dict, clean: dict) -> None:
    assert check(guard, "set_volume", args).clean_args == clean


@pytest.mark.parametrize(
    "value, name", [("save", "save"), ("Ctrl+S", "save"), ("ctrl + v", "paste"),
                    ("Select All", "select_all"), ("control+z", "undo")]
)
def test_shortcut_normalised(guard: SafetyGuard, value: str, name: str) -> None:
    decision = check(guard, "press_shortcut", {"shortcut": value})
    assert decision.verdict is Verdict.ALLOW and decision.clean_args == {"shortcut": name}


def test_app_names_canonical(guard: SafetyGuard) -> None:
    assert check(guard, "open_app", {"name": "नोटपैड"}).clean_args == {"name": "notepad"}


def test_rate_limit(settings: Settings) -> None:
    guard = SafetyGuard(replace(settings, max_actions_per_minute=3),
                        schemas_provider=actions.tool_schemas)
    verdicts = [check(guard, "current_time").verdict for _ in range(5)]
    assert verdicts == [Verdict.ALLOW] * 3 + [Verdict.DENY] * 2
    last = check(guard, "current_time")
    assert last.reason.startswith("rate limit")
    # Denied-for-bad-args calls do not use up the budget.
    guard2 = SafetyGuard(replace(settings, max_actions_per_minute=1),
                         schemas_provider=actions.tool_schemas)
    check(guard2, "open_app", {"name": "cmd"})
    assert check(guard2, "current_time").verdict is Verdict.ALLOW


def test_rate_limiter_window() -> None:
    now = [0.0]
    limiter = RateLimiter(2, clock=lambda: now[0])
    assert limiter.allow() and limiter.allow()
    assert not limiter.allow()
    now[0] = 61.0
    assert limiter.allow()


def test_halt_denies_everything(guard: SafetyGuard) -> None:
    guard.halt("mouse in corner")
    decision = check(guard, "current_time")
    assert decision.verdict is Verdict.DENY and "emergency stop" in decision.reason
    assert not guard.health().ok
    guard.resume()
    assert check(guard, "current_time").verdict is Verdict.ALLOW


def test_without_schemas_allowlist_still_enforced(settings: Settings) -> None:
    guard = SafetyGuard(settings)
    assert check(guard, "open_app", {"name": "crome"}).clean_args == {"name": "chrome"}
    assert check(guard, "press_shortcut", {"shortcut": "alt+f4"}).verdict is Verdict.DENY
    assert check(guard, "run_command", {}).verdict is Verdict.DENY


def test_custom_allowlist_path(settings: Settings, tmp_path: Path) -> None:
    path = tmp_path / "allow.yaml"
    path.write_text("actions:\n  current_time: {risk: high}\n", encoding="utf-8")
    guard = SafetyGuard(settings, allowlist_path=path)
    assert check(guard, "current_time").verdict is Verdict.CONFIRM
    assert check(guard, "open_app", {"name": "notepad"}).verdict is Verdict.DENY


def test_guard_bug_denies(guard: SafetyGuard, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(guard, "_clean_args", boom)
    decision = check(guard, "current_time")
    assert decision.verdict is Verdict.DENY and "safety check failed" in decision.reason


def test_turn_id_passed_through(guard: SafetyGuard) -> None:
    assert guard.check(ToolCall("current_time"), turn_id="t0001").turn_id == "t0001"


def test_validate_args_direct() -> None:
    params = {"properties": {"level": {"type": "integer", "minimum": 0}}, "required": ["level"]}
    assert validate_args(params, {"level": "3"}) == {"level": 3}
    with pytest.raises(ArgumentError):
        validate_args(params, {})
    with pytest.raises(ArgumentError):
        validate_args(params, {"level": -1})

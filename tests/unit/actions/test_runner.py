"""The action runner (timeouts, no exceptions escape, facts) and the fake runner."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from actions import FakeActionRunner, registry
from actions.registry import ACTIONS, ActionError, ActionSpec
from actions.runner import ActionRunner
from core.config import Settings
from core.events import ActionResult, ToolCall
from core.interfaces import ActionRunner as ActionRunnerProtocol


@pytest.fixture
def runner(settings: Settings) -> Iterator[ActionRunner]:
    r = ActionRunner(settings)
    yield r
    r.stop()


def _swap(monkeypatch: pytest.MonkeyPatch, name: str, func: object) -> None:
    monkeypatch.setitem(ACTIONS, name, replace(ACTIONS[name], func=func))


def test_implements_protocol(runner: ActionRunner) -> None:
    proto: ActionRunnerProtocol = runner
    assert proto.tool_schemas() == registry.tool_schemas()


def test_ok_result_has_fact(runner: ActionRunner, no_side_effects: dict[str, MagicMock]) -> None:
    result = runner.run(ToolCall("open_app", {"name": "notepad"}, call_id="c1"))
    assert isinstance(result, ActionResult)
    assert result.ok and result.fact == "opened notepad"
    assert result.result == "opened notepad"
    assert result.duration_ms >= 0 and result.extra["call_id"] == "c1"
    assert not result.informational


def test_volume_fact(runner: ActionRunner, fake_volume: MagicMock) -> None:
    assert runner.run(ToolCall("set_volume", {"level": 40})).fact == "volume 40"


def test_informational_result(runner: ActionRunner) -> None:
    result = runner.run(ToolCall("current_time"))
    assert result.ok and result.informational
    assert result.fact.startswith("time ")
    assert set(result.result) == {"time", "date"}


def test_not_installed_is_error_with_fact(
    runner: ActionRunner, no_side_effects: dict[str, MagicMock]
) -> None:
    result = runner.run(ToolCall("open_app", {"name": "photoshop"}))
    assert result.status == "error"
    assert result.fact == "photoshop not installed"
    no_side_effects["startfile"].assert_not_called()


def test_unknown_action(runner: ActionRunner) -> None:
    result = runner.run(ToolCall("format_disk", {"drive": "C"}))
    assert result.status == "error" and "unknown action" in result.fact


def test_bad_arguments_do_not_raise(runner: ActionRunner) -> None:
    result = runner.run(ToolCall("set_volume", {"volume": 3}))
    assert result.status == "error" and result.fact == "set volume got bad arguments"


def test_unexpected_exception_is_error(
    runner: ActionRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _swap(monkeypatch, "current_time", MagicMock(side_effect=OSError("disk full")))
    result = runner.run(ToolCall("current_time"))
    assert result.status == "error"
    assert result.fact == "current time failed"
    assert "disk full" in result.extra["error"]
    assert not runner.health().ok


def test_action_error_keeps_health(runner: ActionRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    _swap(monkeypatch, "current_time", MagicMock(side_effect=ActionError("no clock")))
    assert runner.run(ToolCall("current_time")).fact == "no clock"
    assert runner.health().ok


def test_failsafe_is_cancelled(runner: ActionRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    class FailSafeException(Exception):
        pass

    _swap(monkeypatch, "scroll", MagicMock(side_effect=FailSafeException("corner")))
    result = runner.run(ToolCall("scroll", {"direction": "up"}))
    assert result.status == "cancelled" and "emergency stop" in result.fact


def test_timeout_returns_and_does_not_block(
    runner: ActionRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    _swap(monkeypatch, "current_time", lambda: release.wait(5))
    try:
        result = runner.run(ToolCall("current_time"), timeout_s=0.05)
        assert result.status == "timeout"
        assert result.fact == "current time took too long"
    finally:
        release.set()


def test_default_timeout_from_settings(settings: Settings) -> None:
    runner = ActionRunner(replace(settings, action_timeout_s=0.5))
    assert runner._timeout() == 0.5
    runner.stop()


def test_runs_off_the_caller_thread(runner: ActionRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    _swap(monkeypatch, "current_time", lambda: seen.append(threading.current_thread().name) or {})
    runner.run(ToolCall("current_time"))
    assert seen and seen[0] != threading.current_thread().name


def test_halt_and_resume(runner: ActionRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    func = MagicMock(return_value="volume 3")
    _swap(monkeypatch, "set_volume", func)
    runner.halt("mouse in corner")
    result = runner.run(ToolCall("set_volume", {"level": 3}))
    assert result.status == "cancelled" and not runner.health().ok
    func.assert_not_called()
    runner.resume()
    assert runner.run(ToolCall("set_volume", {"level": 3})).ok


def test_stopped_runner_restarts_pool(runner: ActionRunner) -> None:
    runner.stop()
    assert runner.run(ToolCall("current_time")).ok


def test_start_scans_start_menu(runner: ActionRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    from actions import apps

    scan = MagicMock(side_effect=OSError("boom"))
    monkeypatch.setattr(apps, "scan_start_menu", scan)
    runner.start()  # a failed scan must not raise
    scan.assert_called_once()


def test_fact_formatter_failure_is_still_ok(
    runner: ActionRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = ActionSpec("weird", lambda: 1, "x", {"properties": {}}, fact=lambda r: 1 / 0)
    monkeypatch.setitem(ACTIONS, "weird", spec)
    result = runner.run(ToolCall("weird"))
    assert result.ok and result.fact == "weird done"


# -- fake runner ----------------------------------------------------------------------


def test_fake_runner_records_and_succeeds(no_side_effects: dict[str, MagicMock]) -> None:
    fake = FakeActionRunner()
    proto: ActionRunnerProtocol = fake
    assert proto.tool_schemas() == registry.tool_schemas()
    result = fake.run(ToolCall("shutdown_pc"))
    assert result.ok and fake.names == ["shutdown_pc"]
    assert fake.run(ToolCall("current_time")).informational
    assert fake.run(ToolCall("system_status")).fact.startswith("battery 80%")
    assert fake.health().ok
    for mock in no_side_effects.values():
        mock.assert_not_called()


def test_fake_runner_scripted_outcome() -> None:
    fake = FakeActionRunner({"open_app": ("error", "photoshop not installed")})
    result = fake.run(ToolCall("open_app", {"name": "photoshop"}))
    assert result.status == "error" and result.fact == "photoshop not installed"
    assert fake.calls == [ToolCall("open_app", {"name": "photoshop"})]

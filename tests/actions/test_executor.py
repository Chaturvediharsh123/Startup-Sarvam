from __future__ import annotations

import asyncio

import pytest

from actions.base import AdapterOutcome
from actions.config import ActionSettings
from actions.errors import AdapterError, TargetNotFoundError
from actions.executor import ActionExecutor, SyncActionExecutor
from actions.models import Action, Evidence

from .conftest import FakeAdapter, browser_target, sleep_forever

CLICK = {"type": "click", "target": browser_target(role="button", name="Download")}
READ = {"type": "read", "target": browser_target(text="Total")}


async def test_successful_action_returns_structured_result(executor, adapters):
    result = await executor.execute(CLICK)
    assert result.success and result.error is None
    assert result.action_type == "click"
    assert result.adapter == "browser"
    assert result.attempts == 1
    assert result.duration_ms >= 0
    assert result.risk == "medium"
    assert result.started_at <= result.finished_at
    assert any(e.type == "url" for e in result.evidence)  # observe() evidence
    assert len(adapters["browser"].calls) == 1


async def test_validation_error_is_returned_not_raised_and_hides_input(executor, adapters):
    result = await executor.execute({"type": "type", "parameters": {"text": "s3cret-password", "bogus": 1}})
    assert not result.success
    assert result.error.error_type == "ValidationError"
    assert result.retryable is False and result.attempts == 0
    assert "s3cret-password" not in result.model_dump_json()
    assert not any(a.calls for a in adapters.values())


async def test_unregistered_capability(executor):
    result = await executor.execute({"type": "execute_shell", "parameters": {"command": "dir"}})
    assert result.error.error_type == "UnsupportedActionError"
    assert result.retryable is False


async def test_disabled_capability(adapters, settings):
    settings.disabled_capabilities = ["download"]
    executor = ActionExecutor(adapters, settings=settings)
    result = await executor.execute({"type": "download", "parameters": {"url": "https://x.test/a.pdf"}})
    assert result.error.error_type == "UnsupportedActionError"


async def test_app_allow_list(executor, adapters):
    result = await executor.execute({"type": "open_app", "parameters": {"app": "regedit"}})
    assert result.error.error_type == "PermissionError"
    ok = await executor.execute({"type": "open_app", "parameters": {"app": "Notepad"}})
    assert ok.success and ok.adapter == "windows"


async def test_key_and_hotkey_allow_lists(executor):
    assert (await executor.execute({"type": "hotkey", "parameters": {"keys": "ctrl+alt+delete"}})).error.error_type == "PermissionError"
    assert (await executor.execute({"type": "hotkey", "parameters": {"keys": "win+r"}})).error.error_type == "PermissionError"
    assert (await executor.execute({"type": "hotkey", "parameters": {"keys": "Control+S"}})).success
    assert (await executor.execute({"type": "press_key", "parameters": {"key": "enter"}})).success
    assert (await executor.execute({"type": "press_key", "parameters": {"key": "a"}})).success
    assert (await executor.execute({"type": "press_key", "parameters": {"key": "printscreen"}})).error.error_type == "PermissionError"


async def test_missing_or_unavailable_adapter(settings):
    executor = ActionExecutor({"browser": FakeAdapter("browser", available=False)}, settings=settings)
    assert (await executor.execute(CLICK)).error.error_type == "UnsupportedActionError"
    assert (await executor.execute({"type": "open_app", "parameters": {"app": "notepad"}})).error.error_type == "UnsupportedActionError"


async def test_timeout_on_non_idempotent_action_is_not_retried(executor, adapters):
    adapters["browser"].script = [sleep_forever, sleep_forever]
    result = await executor.execute({**CLICK, "timeout_s": 0.2})
    assert result.error.error_type == "TimeoutError"
    assert result.attempts == 1
    assert result.retryable is False  # the click may already have happened
    assert result.duration_ms < 2000


async def test_timeout_on_idempotent_action_is_retried(executor, adapters):
    adapters["browser"].script = [sleep_forever, AdapterOutcome(output={"text": "ok"})]
    result = await executor.execute({**READ, "timeout_s": 0.2, "retry_policy": {"max_attempts": 2, "backoff_s": 0}})
    assert result.success and result.attempts == 2


async def test_target_not_found_is_retried_even_for_click(executor, adapters):
    adapters["browser"].script = [TargetNotFoundError("not yet"), AdapterOutcome(output={"matches": 1})]
    result = await executor.execute({**CLICK, "retry_policy": {"max_attempts": 2, "backoff_s": 0}})
    assert result.success and result.attempts == 2


async def test_retry_attempts_are_capped_by_settings(adapters, settings):
    settings.max_attempts_cap = 2
    executor = ActionExecutor(adapters, settings=settings)
    adapters["browser"].script = [TargetNotFoundError("x")] * 5
    result = await executor.execute({**CLICK, "retry_policy": {"max_attempts": 5, "backoff_s": 0}})
    assert result.attempts == 2
    assert result.error.error_type == "TargetNotFoundError"
    assert result.retryable is True


async def test_unexpected_exception_becomes_adapter_error(executor, adapters):
    adapters["browser"].script = [RuntimeError("boom")]
    result = await executor.execute(READ)
    assert result.error.error_type == "AdapterError"
    assert result.error.adapter == "browser"
    assert result.error.details["exception"] == "RuntimeError"
    assert result.attempts == 1  # AdapterError is not retryable by default


async def test_structured_error_carries_target(executor, adapters):
    adapters["browser"].script = [AdapterError("element detached")]
    result = await executor.execute(CLICK)
    assert result.error.target["name"] == "Download"
    assert result.error.message == "element detached"


async def test_unsatisfied_precondition_blocks_execution(executor, adapters):
    adapters["browser"].condition_status["element_visible"] = "unsatisfied"
    action = {**CLICK, "preconditions": [{"kind": "element_visible", "target": CLICK["target"],
                                          "description": "Download button is visible"}]}
    result = await executor.execute(action)
    assert result.error.error_type == "PreconditionFailedError"
    assert "Download button is visible" in result.error.message
    assert adapters["browser"].calls == []
    assert result.precondition_checks[0].status == "unsatisfied"


async def test_precondition_wait_polls_until_satisfied(executor, adapters):
    statuses = iter(["unsatisfied", "unsatisfied", "satisfied"])
    adapters["browser"].condition_status["url_contains"] = lambda: next(statuses, "satisfied")
    action = {**CLICK, "preconditions": [{"kind": "url_contains", "value": "/bills", "wait_s": 2}]}
    result = await executor.execute(action)
    assert result.success


async def test_unverifiable_precondition_does_not_block(executor, adapters):
    action = {**CLICK, "preconditions": [{"kind": "custom", "description": "User is logged in"}]}
    result = await executor.execute(action)
    assert result.success
    assert result.precondition_checks[0].status == "unverified"


async def test_execution_success_is_separate_from_expected_outcome(executor, adapters):
    """Rule 11: click succeeded, file did not appear."""
    adapters["browser"].condition_status["url_contains"] = "unsatisfied"
    action = {**CLICK, "expected_outcomes": [
        {"kind": "url_contains", "value": "/downloaded"},
        {"kind": "custom", "description": "PDF download begins"},
    ]}
    result = await executor.execute(action)
    assert result.success is True
    assert result.expected_outcomes_met is False
    assert [c.status for c in result.outcome_checks] == ["unsatisfied", "unverified"]


async def test_download_and_file_outcomes(executor, adapters, tmp_path):
    pdf = tmp_path / "bill.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    adapters["browser"].script = [AdapterOutcome(output={"path": str(pdf)},
                                                 evidence=[Evidence(type="download_path", value=str(pdf))])]
    action = {"type": "download", "target": browser_target(role="link", name="Download"), "expected_outcomes": [
        {"kind": "download_completed"},
        {"kind": "file_exists", "value": str(pdf)},
        {"kind": "file_exists", "value": str(tmp_path / "missing.pdf")},
    ]}
    result = await executor.execute(action)
    assert [c.status for c in result.outcome_checks] == ["satisfied", "satisfied", "unsatisfied"]
    assert any(e.type == "download_path" and e.value == str(pdf) for e in result.evidence)


async def test_visual_fallback_is_explicit_and_recorded(executor, adapters):
    adapters["browser"].script = [TargetNotFoundError("no element")]
    target = browser_target(role="button", name="Pay", point={"x": 10, "y": 20}, allow_visual_fallback=True)
    result = await executor.execute({"type": "click", "target": target, "retry_policy": {"max_attempts": 1}})
    assert result.success and result.adapter == "visual"
    assert adapters["screen"].calls[0].target.surface == "screen"
    assert adapters["screen"].calls[0].target.point.x == 10
    assert result.evidence[0].type == "fallback"


async def test_no_visual_fallback_without_opt_in(executor, adapters):
    adapters["browser"].script = [TargetNotFoundError("no element")]
    target = browser_target(role="button", name="Pay", point={"x": 10, "y": 20})
    result = await executor.execute({"type": "click", "target": target, "retry_policy": {"max_attempts": 1}})
    assert result.error.error_type == "TargetNotFoundError"
    assert adapters["screen"].calls == []


async def test_wait_seconds_and_until(executor, adapters):
    result = await executor.execute({"type": "wait", "parameters": {"seconds": 0.05}})
    assert result.success and result.adapter == "executor"

    adapters["browser"].condition_status["title_contains"] = "satisfied"
    result = await executor.execute({"type": "wait", "parameters": {"until": {"kind": "title_contains", "value": "Bills"}}})
    assert result.success

    adapters["browser"].condition_status["title_contains"] = "unsatisfied"
    result = await executor.execute({"type": "wait", "timeout_s": 0.3, "retry_policy": {"max_attempts": 1},
                                     "parameters": {"until": {"kind": "title_contains", "value": "Bills"}}})
    assert result.error.error_type == "VerificationError"


async def test_failure_screenshot_evidence(adapters, settings, tmp_path):
    settings.screenshot_on_failure = True
    adapters["browser"].screenshot_path = tmp_path / "fail.png"
    adapters["browser"].script = [AdapterError("broken")]
    result = await ActionExecutor(adapters, settings=settings).execute(CLICK)
    shots = [e for e in result.evidence if e.type == "screenshot"]
    assert shots and shots[0].metadata["reason"] == "failure"


async def test_close_closes_every_adapter_once(executor, adapters):
    await executor.close()
    assert all(a.closed for a in adapters.values())


def test_sync_executor(adapters, settings):
    sync = SyncActionExecutor(ActionExecutor(adapters, settings=settings))
    try:
        assert sync.execute(Action.model_validate(CLICK)).success
    finally:
        sync.close()


async def test_timeout_is_clamped_to_max(adapters):
    settings = ActionSettings(max_timeout_s=0.2, screenshot_on_failure=False, browser_user_data_dir=None, _env_file=None)
    adapters["browser"].script = [sleep_forever]
    result = await ActionExecutor(adapters, settings=settings).execute({**CLICK, "timeout_s": 500})
    assert result.error.error_type == "TimeoutError"


async def test_concurrent_execution_is_independent(executor):
    results = await asyncio.gather(*(executor.execute(READ) for _ in range(5)))
    assert len({r.action_id for r in results}) == 5

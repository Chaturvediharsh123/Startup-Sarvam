"""ThreadRunner recovery from stuck UI calls, and executor warm-up."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from actions.base import Adapter, AdapterOutcome, ExecutionContext, ThreadRunner
from actions.errors import AdapterError
from actions.executor import ActionExecutor
from actions.models import Action, ActionType

from .conftest import FakeAdapter


async def test_runner_returns_results_and_propagates_errors():
    runner = ThreadRunner("t")
    assert await runner.run(lambda x: x * 2, 21) == 42
    with pytest.raises(ZeroDivisionError):
        await runner.run(lambda: 1 / 0)
    runner.shutdown()


async def test_stuck_call_is_abandoned_and_next_call_runs_on_fresh_thread():
    runner = ThreadRunner("t")
    release = threading.Event()
    first_thread = await runner.run(threading.get_ident)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(runner.run(release.wait), timeout=0.1)
    assert runner.abandoned_threads == 1

    started = time.monotonic()
    second_thread = await runner.run(threading.get_ident)
    assert time.monotonic() - started < 1.0  # not queued behind the stuck call
    assert second_thread != first_thread
    release.set()
    runner.shutdown()


async def test_calls_queued_behind_a_stuck_call_are_refused_not_run_late():
    runner = ThreadRunner("t")
    release = threading.Event()
    ran: list[str] = []
    stuck = asyncio.ensure_future(runner.run(release.wait))
    queued = asyncio.ensure_future(runner.run(lambda: ran.append("late")))
    await asyncio.sleep(0.05)
    stuck.cancel()  # what the executor's timeout does
    with pytest.raises(asyncio.CancelledError):
        await stuck
    release.set()
    with pytest.raises(AdapterError) as info:
        await queued
    assert ran == [] and info.value.may_have_side_effects is False
    runner.shutdown()


async def test_cancelling_a_call_that_has_not_started_keeps_the_thread():
    runner = ThreadRunner("t")
    release = threading.Event()
    busy = asyncio.ensure_future(runner.run(release.wait))
    waiting = asyncio.ensure_future(runner.run(lambda: "never"))
    await asyncio.sleep(0.05)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert runner.abandoned_threads == 0
    release.set()
    assert await busy is True
    runner.shutdown()


class BlockingAdapter(Adapter):
    """Sync adapter whose first call hangs, like a UI Automation call on a frozen window."""

    name = "blocking"
    supports = frozenset(ActionType)

    def __init__(self) -> None:
        self.runner = ThreadRunner("blocking")
        self.release = threading.Event()
        self.calls = 0

    def _work(self) -> AdapterOutcome:
        self.calls += 1
        if self.calls == 1:
            self.release.wait()
        return AdapterOutcome(output={"call": self.calls})

    async def execute(self, action: Action, ctx: ExecutionContext) -> AdapterOutcome:
        return await self.runner.run(self._work)


async def test_executor_recovers_after_a_hung_adapter_call(settings):
    adapter = BlockingAdapter()
    executor = ActionExecutor({"screen": adapter}, settings=settings)
    hung = await executor.execute({"type": "hotkey", "parameters": {"keys": "ctrl+s"}, "timeout_s": 0.2})
    assert hung.error.error_type == "TimeoutError" and hung.retryable is False

    started = time.monotonic()
    ok = await executor.execute({"type": "hotkey", "parameters": {"keys": "ctrl+s"}, "timeout_s": 2})
    assert ok.success and time.monotonic() - started < 1.0
    adapter.release.set()
    adapter.runner.shutdown()


class BrokenWarmUp(FakeAdapter):
    async def warm_up(self) -> None:
        raise RuntimeError("no display")


async def test_warm_up_reports_per_adapter(settings):
    adapters = {"browser": FakeAdapter("browser"), "window": FakeAdapter("windows", available=False),
                "screen": BrokenWarmUp("visual")}
    report = await ActionExecutor(adapters, settings=settings).warm_up()
    assert report == {"browser": "ok", "windows": "unavailable", "visual": "RuntimeError: no display"}

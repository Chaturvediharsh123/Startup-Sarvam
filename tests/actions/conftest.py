from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

import pytest

from actions.base import Adapter, AdapterOutcome, ExecutionContext
from actions.config import ActionSettings
from actions.executor import ActionExecutor
from actions.models import Action, ActionType, Condition, ConditionCheck, ConditionStatus, Evidence, Target

ALL_TYPES = frozenset(ActionType)


class FakeAdapter(Adapter):
    """Scriptable adapter: each execute() pops the next step (outcome, exception or coroutine fn)."""

    def __init__(self, name: str = "fake", supports: frozenset[ActionType] = ALL_TYPES, available: bool = True) -> None:
        self.name = name  # type: ignore[misc]
        self.supports = supports  # type: ignore[misc]
        self._available = available
        self.script: list[Any] = []
        self.calls: list[Action] = []
        self.condition_status: dict[str, ConditionStatus | Callable[[], ConditionStatus]] = {}
        self.screenshot_path: Path | None = None
        self.closed = False

    def available(self) -> bool:
        return self._available

    async def execute(self, action: Action, ctx: ExecutionContext) -> AdapterOutcome:
        self.calls.append(action)
        step = self.script.pop(0) if self.script else AdapterOutcome(output={"ok": True})
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            return await step(action, ctx)
        return step

    async def check(self, condition: Condition, ctx: ExecutionContext) -> ConditionCheck:
        status = self.condition_status.get(condition.kind, "unverified")
        if callable(status):
            status = status()
        return ConditionCheck(condition=condition, status=status, observed=f"{self.name}:{condition.kind}")

    async def observe(self) -> list[Evidence]:
        return [Evidence(type="url", value=f"https://{self.name}.test/")]

    async def screenshot(self, path: Path, target: Target | None = None) -> Path | None:
        if self.screenshot_path is None:
            return None
        return self.screenshot_path

    async def close(self) -> None:
        self.closed = True


async def sleep_forever(action: Action, ctx: ExecutionContext) -> AdapterOutcome:
    await asyncio.sleep(10)
    return AdapterOutcome()


@pytest.fixture
def settings(tmp_path: Path) -> ActionSettings:
    return ActionSettings(
        evidence_dir=tmp_path / "evidence",
        download_dir=tmp_path / "downloads",
        browser_user_data_dir=None,
        screenshot_on_failure=False,
        _env_file=None,
    )


@pytest.fixture
def adapters() -> dict[str, FakeAdapter]:
    return {"browser": FakeAdapter("browser"), "window": FakeAdapter("windows"), "screen": FakeAdapter("visual")}


@pytest.fixture
def executor(adapters: dict[str, FakeAdapter], settings: ActionSettings) -> ActionExecutor:
    return ActionExecutor(adapters, settings=settings)  # type: ignore[arg-type]


def browser_target(**kw: Any) -> dict[str, Any]:
    return {"surface": "browser", **kw}

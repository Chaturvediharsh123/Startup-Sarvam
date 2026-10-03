"""Adapter interface shared by the Playwright, Windows UI Automation and visual adapters.

Adapters only interact with the computer and report what they observed. They contain no
planning, no memory access, no user preferences and no business/workflow knowledge.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from abc import ABC, abstractmethod
from concurrent.futures import Future
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Callable, ClassVar, TypeVar

from actions.config import ActionSettings
from actions.errors import AdapterError
from actions.models import Action, ActionType, Condition, ConditionCheck, Evidence, Target

T = TypeVar("T")
logger = logging.getLogger("actions.runner")


@dataclass
class ExecutionContext:
    """Per-attempt execution budget and settings handed to an adapter."""

    settings: ActionSettings
    timeout_s: float
    deadline: float = field(default=0.0)

    def __post_init__(self) -> None:
        if not self.deadline:
            self.deadline = time.monotonic() + self.timeout_s

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def find_timeout(self, share: float = 0.6, minimum: float = 0.5) -> float:
        """Time to spend locating a target, leaving the rest of the budget for the interaction."""
        return max(minimum, self.remaining() * share)

    def evidence_path(self, action_id: str, label: str, ext: str = "png") -> Path:
        directory = Path(self.settings.evidence_dir)
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{action_id}_{label}_{int(time.time() * 1000)}.{ext}"


@dataclass
class AdapterOutcome:
    output: Any = None
    evidence: list[Evidence] = field(default_factory=list)


class Adapter(ABC):
    name: ClassVar[str]
    supports: ClassVar[frozenset[ActionType]]

    def available(self) -> bool:
        """False when the backing library or platform is missing; the router then refuses
        actions for this adapter with UnsupportedActionError instead of crashing."""
        return True

    @abstractmethod
    async def execute(self, action: Action, ctx: ExecutionContext) -> AdapterOutcome: ...

    async def check(self, condition: Condition, ctx: ExecutionContext) -> ConditionCheck:
        return ConditionCheck(condition=condition, status="unverified", detail=f"{self.name} cannot check {condition.kind}")

    async def screenshot(self, path: Path, target: Target | None = None) -> Path | None:
        return None

    async def observe(self) -> list[Evidence]:
        """Cheap state evidence captured after an action (e.g. current URL and title)."""
        return []

    async def warm_up(self) -> None:
        """Pay one-time setup costs (library imports, COM init) before the first real action."""
        return None

    async def close(self) -> None:
        return None


class _Worker:
    """One daemon thread draining a queue. Daemon, so a call stuck forever never blocks exit."""

    def __init__(self, name: str, initializer: Callable[[], None] | None) -> None:
        self._queue: queue.SimpleQueue[tuple[Future[Any], Callable[[], Any]] | None] = queue.SimpleQueue()
        self._initializer = initializer
        self.stopped = False
        self.thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        if self._initializer is not None:
            try:
                self._initializer()
            except Exception:
                logger.exception("worker initializer failed")
        while (item := self._queue.get()) is not None:
            future, fn = item
            if self.stopped:
                # Queued behind a call that timed out: refuse rather than send input late.
                future.set_exception(AdapterError("worker restarted after a timeout; action was not executed",
                                                  retryable=True, may_have_side_effects=False))
                continue
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn())
            except BaseException as exc:
                future.set_exception(exc)

    def submit(self, fn: Callable[[], Any]) -> Future[Any]:
        future: Future[Any] = Future()
        self._queue.put((future, fn))
        return future

    def stop(self) -> None:
        self.stopped = True
        self._queue.put(None)


class ThreadRunner:
    """Runs blocking UI-library calls on one dedicated thread.

    UI Automation (COM) objects must stay on the thread that created them, and a single
    thread also serialises mouse/keyboard input. A thread cannot be killed, so when a call
    is still running after its await was cancelled (the executor's timeout), that thread is
    abandoned and a fresh one takes over; the stuck call finishes (or not) on its own.
    Adapters still pass library timeouts so this stays the exception.
    """

    def __init__(self, name: str, initializer: Callable[[], None] | None = None) -> None:
        self._name = name
        self._initializer = initializer
        self._lock = threading.Lock()
        self._worker = _Worker(name, initializer)
        self.abandoned_threads = 0

    async def run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        worker = self._worker
        future = worker.submit(partial(fn, *args, **kwargs))
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            if not future.done() and not future.cancel():  # running, i.e. blocked inside the UI call
                self._replace(worker)
            raise

    def _replace(self, worker: _Worker) -> None:
        with self._lock:
            if self._worker is not worker:
                return
            self._worker = _Worker(self._name, self._initializer)
            self.abandoned_threads += 1
        worker.stop()
        logger.warning("%s worker thread abandoned after a timeout; started a fresh one", self._name)

    def shutdown(self) -> None:
        self._worker.stop()

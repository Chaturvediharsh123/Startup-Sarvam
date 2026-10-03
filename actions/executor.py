"""ActionExecutor: the only path from the rest of the system to the computer.

Pipeline: validate -> capability check -> technical restrictions -> route -> preconditions
-> execute (bounded timeout, limited safe retries, optional visual fallback) -> evidence
-> expected-outcome observations -> ActionResult.

``execute`` never raises for action-level problems; every failure becomes a structured
``ActionResult`` so the planner can decide what to do next.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from pydantic import ValidationError

from actions.base import Adapter, AdapterOutcome, ExecutionContext
from actions.config import ActionSettings
from actions.errors import (
    ActionError,
    ActionPermissionError,
    ActionTimeoutError,
    ActionValidationError,
    AdapterError,
    PreconditionFailedError,
    TargetNotFoundError,
    UnsupportedActionError,
    VerificationError,
)
from actions.models import (
    Action,
    ActionResult,
    ActionType,
    Condition,
    ConditionCheck,
    Evidence,
    Surface,
    WaitParams,
    normalize_hotkey,
    normalize_key,
    utcnow,
)
from actions.registry import ActionRegistry, Capability
from actions.router import ActionRouter

logger = logging.getLogger("actions.executor")

_POLL_INTERVAL_S = 0.25
_CHECK_TIMEOUT_S = 5.0
_SCREENSHOT_TIMEOUT_S = 10.0


class ActionExecutor:
    def __init__(
        self,
        adapters: Mapping[Surface, Adapter],
        settings: ActionSettings | None = None,
        registry: ActionRegistry | None = None,
    ) -> None:
        self.settings = settings or ActionSettings()
        self.registry = registry or ActionRegistry(disabled=self.settings.disabled_capabilities)
        self.router = ActionRouter(adapters, self.registry)
        self._adapters = dict(adapters)
        self._allowed_keys = {normalize_key(k) for k in self.settings.allowed_keys}
        self._allowed_hotkeys: set[str] = set()
        for combo in self.settings.allowed_hotkeys:
            try:
                self._allowed_hotkeys.add("+".join(normalize_hotkey(combo.split("+"))))
            except ValueError:
                logger.warning("ignoring invalid allowed_hotkeys entry %r", combo)

    async def __aenter__(self) -> ActionExecutor:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------------ public API

    async def execute(self, action: Action | Mapping[str, Any]) -> ActionResult:
        started_at = utcnow()
        t0 = time.monotonic()
        action_id = action.action_id if isinstance(action, Action) else str(dict(action).get("action_id") or "unvalidated")

        try:
            action = self._validate(action)
            action_id = action.action_id
            cap = self.registry.check(action)
            self._enforce_restrictions(action)
            chain = self.router.route(action)
        except ActionError as exc:
            return self._finish(action_id, action if isinstance(action, Action) else None, started_at, t0,
                                error=exc, retryable=False, attempts=0)

        risk = self.registry.effective_risk(action)
        timeout = self._timeout(action)
        logger.info("action.start id=%s type=%s risk=%s", action_id, action.type.value, risk,
                    extra={"action_event": {"event": "start", "action": action.log_view(), "risk": risk}})

        pre_checks = await self._check_conditions(action.preconditions, produced=None)
        unmet = [c for c in pre_checks if c.status == "unsatisfied"]
        if unmet:
            exc = PreconditionFailedError(
                "precondition not met: " + "; ".join(c.condition.description or c.condition.kind for c in unmet),
                target=action.target.summary() if action.target else None,
                details={"unmet": [c.model_dump(mode="json") for c in unmet]},
            )
            evidence = await self._failure_evidence(action, chain)
            return self._finish(action_id, action, started_at, t0, error=exc, retryable=True, attempts=0,
                                risk=risk, pre_checks=pre_checks, evidence=evidence)

        max_attempts = min(action.retry_policy.max_attempts, self.settings.max_attempts_cap)
        attempts = 0
        outcome: AdapterOutcome | None = None
        adapter_name: str | None = None
        error: ActionError | None = None
        while attempts < max_attempts:
            attempts += 1
            try:
                outcome, adapter_name = await self._attempt(action, chain, timeout)
                error = None
                break
            except ActionError as exc:
                error = exc
                if attempts >= max_attempts or not self._may_retry(exc, cap):
                    break
                logger.info("action.retry id=%s attempt=%d error=%s", action_id, attempts, exc.error_type)
                await asyncio.sleep(action.retry_policy.backoff_s * attempts)

        if outcome is None:
            assert error is not None
            evidence = list(error.evidence) + await self._failure_evidence(action, chain)
            return self._finish(action_id, action, started_at, t0, error=error, retryable=self._may_retry(error, cap),
                                attempts=attempts, risk=risk, pre_checks=pre_checks, evidence=evidence,
                                adapter=error.adapter)

        evidence = list(outcome.evidence) + await self._observe(adapter_name)
        outcome_checks = await self._check_conditions(action.expected_outcomes, produced=evidence)
        return self._finish(action_id, action, started_at, t0, output=outcome.output, attempts=attempts, risk=risk,
                            pre_checks=pre_checks, outcome_checks=outcome_checks, evidence=evidence,
                            adapter=adapter_name)

    async def warm_up(self) -> dict[str, str]:
        """Run each available adapter's one-time setup now (call at app start-up) so the first
        spoken command is not slowed down. The browser is not launched here. Never raises;
        returns ``{adapter: "ok" | "unavailable" | "<error>"}``."""
        report: dict[str, str] = {}
        for adapter in {id(a): a for a in self._adapters.values()}.values():
            if not adapter.available():
                report[adapter.name] = "unavailable"
                continue
            started = time.monotonic()
            try:
                await asyncio.wait_for(adapter.warm_up(), timeout=self.settings.max_timeout_s)
                report[adapter.name] = "ok"
            except Exception as exc:
                report[adapter.name] = f"{type(exc).__name__}: {exc}"
            logger.info("adapter.warm_up %s %s in %.2fs", adapter.name, report[adapter.name], time.monotonic() - started)
        return report

    async def close(self) -> None:
        for adapter in {id(a): a for a in self._adapters.values()}.values():
            try:
                await adapter.close()
            except Exception:  # closing must never mask the caller's own errors
                logger.exception("adapter %s failed to close", adapter.name)

    # ------------------------------------------------------------------ validation / policy

    def _validate(self, raw: Action | Mapping[str, Any]) -> Action:
        if isinstance(raw, Action):
            return raw
        if not isinstance(raw, Mapping):
            raise ActionValidationError("action must be an Action or a mapping")
        action_type = raw.get("type")
        if not self.registry.is_registered(str(getattr(action_type, "value", action_type))):
            raise UnsupportedActionError(f"capability '{action_type}' is not registered")
        try:
            return Action.model_validate(dict(raw))
        except ValidationError as exc:
            # include_input=False keeps typed text (possibly a password) out of results and logs
            errors = exc.errors(include_url=False, include_input=False, include_context=False)
            summary = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in errors)
            raise ActionValidationError(f"invalid action: {summary}", details={"errors": errors}) from None

    def _enforce_restrictions(self, action: Action) -> None:
        params = action.parameters
        if action.type in (ActionType.OPEN_APP, ActionType.CLOSE_APP) and params.app not in self.settings.apps:
            raise ActionPermissionError(f"app '{params.app}' is not in the allow-list",
                                        details={"allowed": sorted(self.settings.apps)})
        if action.type is ActionType.PRESS_KEY:
            key = params.key
            if key not in self._allowed_keys and not (len(key) == 1 and key.isalnum()):
                raise ActionPermissionError(f"key '{key}' is not allowed")
        if action.type is ActionType.HOTKEY and params.combo not in self._allowed_hotkeys:
            raise ActionPermissionError(f"hotkey '{params.combo}' is not allowed",
                                        details={"allowed": sorted(self._allowed_hotkeys)})

    def _timeout(self, action: Action) -> float:
        timeout = action.timeout_s or self.settings.default_timeout_s
        if action.type is ActionType.WAIT and action.parameters.seconds is not None:
            timeout = max(timeout, action.parameters.seconds + 1.0)
        return min(timeout, self.settings.max_timeout_s)

    @staticmethod
    def _may_retry(exc: ActionError, cap: Capability) -> bool:
        """Transient failures are retried only if repeating cannot duplicate an effect."""
        return exc.retryable and (cap.idempotent or not exc.may_have_side_effects)

    # ------------------------------------------------------------------ execution

    async def _attempt(self, action: Action, chain: list[Adapter], timeout: float) -> tuple[AdapterOutcome, str]:
        ctx = ExecutionContext(self.settings, timeout)
        if action.type is ActionType.WAIT:
            return await self._wait(action.parameters, ctx), "executor"
        primary = chain[0]
        try:
            return await self._run_adapter(primary, action, ctx), primary.name
        except TargetNotFoundError as exc:
            if len(chain) < 2:
                raise
            visual = chain[1]
            outcome = await self._run_adapter(visual, self.router.visual_fallback_action(action), ctx)
            outcome.evidence.insert(0, Evidence(type="fallback", value=f"{primary.name}->{visual.name}",
                                                metadata={"reason": exc.message}))
            return outcome, visual.name

    async def _run_adapter(self, adapter: Adapter, action: Action, ctx: ExecutionContext) -> AdapterOutcome:
        target = action.target.summary() if action.target else None
        remaining = ctx.remaining()
        if remaining <= 0:
            raise ActionTimeoutError("no time left in the action budget", adapter=adapter.name, target=target,
                                     may_have_side_effects=False)
        try:
            return await asyncio.wait_for(adapter.execute(action, ctx), timeout=remaining)
        except asyncio.TimeoutError:
            raise ActionTimeoutError(f"{action.type.value} exceeded {ctx.timeout_s:.1f}s", adapter=adapter.name,
                                     target=target) from None
        except ActionError as exc:
            exc.adapter = exc.adapter or adapter.name
            exc.target = exc.target or target
            raise
        except Exception as exc:
            raise AdapterError(f"{type(exc).__name__}: {exc}", adapter=adapter.name, target=target,
                               details={"exception": type(exc).__name__}) from exc

    async def _wait(self, params: WaitParams, ctx: ExecutionContext) -> AdapterOutcome:
        if params.seconds is not None:
            await asyncio.sleep(params.seconds)
            return AdapterOutcome(output={"waited_s": params.seconds})
        assert params.until is not None
        check = await self._poll(params.until, produced=None, budget=ctx.remaining())
        if check.status == "satisfied":
            return AdapterOutcome(output={"condition": "satisfied", "observed": check.observed})
        if check.status == "unverified":
            raise UnsupportedActionError(f"cannot check condition '{params.until.kind}': {check.detail}")
        raise VerificationError(f"condition '{params.until.kind}' not met within {ctx.timeout_s:.1f}s",
                                details={"observed": check.observed})

    # ------------------------------------------------------------------ conditions

    async def _check_conditions(self, conditions: list[Condition], produced: list[Evidence] | None) -> list[ConditionCheck]:
        return [await self._poll(c, produced, budget=c.wait_s) for c in conditions]

    async def _poll(self, condition: Condition, produced: list[Evidence] | None, budget: float) -> ConditionCheck:
        deadline = time.monotonic() + budget
        while True:
            check = await self._check_one(condition, produced)
            remaining = deadline - time.monotonic()
            if check.status != "unsatisfied" or remaining <= 0:
                return check
            await asyncio.sleep(min(_POLL_INTERVAL_S, remaining))

    async def _check_one(self, condition: Condition, produced: list[Evidence] | None) -> ConditionCheck:
        kind = condition.kind
        if kind == "custom":
            return ConditionCheck(condition=condition, status="unverified", detail="requires higher-level verification")
        if kind == "file_exists":
            exists = Path(condition.value or "").expanduser().exists()
            return ConditionCheck(condition=condition, status="satisfied" if exists else "unsatisfied",
                                  observed=condition.value if exists else None)
        if kind == "download_completed":
            if produced is None:
                return ConditionCheck(condition=condition, status="unverified", detail="only checkable after a download")
            paths = [e.value for e in produced if e.type == "download_path" and Path(e.value).exists()]
            return ConditionCheck(condition=condition, status="satisfied" if paths else "unsatisfied",
                                  observed=paths[0] if paths else None)
        adapter = self.router.adapter_for_condition(condition)
        if adapter is None:
            return ConditionCheck(condition=condition, status="unverified", detail="no adapter available to check this")
        ctx = ExecutionContext(self.settings, _CHECK_TIMEOUT_S)
        try:
            return await asyncio.wait_for(adapter.check(condition, ctx), timeout=_CHECK_TIMEOUT_S)
        except Exception as exc:
            return ConditionCheck(condition=condition, status="unverified", detail=f"check failed: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ evidence

    async def _observe(self, adapter_name: str | None) -> list[Evidence]:
        adapter = next((a for a in self._adapters.values() if a.name == adapter_name), None)
        if adapter is None:
            return []
        try:
            return await asyncio.wait_for(adapter.observe(), timeout=_CHECK_TIMEOUT_S)
        except Exception:
            logger.debug("observe failed for %s", adapter_name, exc_info=True)
            return []

    async def _failure_evidence(self, action: Action, chain: list[Adapter]) -> list[Evidence]:
        if not self.settings.screenshot_on_failure:
            return []
        ctx = ExecutionContext(self.settings, _SCREENSHOT_TIMEOUT_S)
        candidates = [*chain[:1], self.router.adapter("screen")]
        for adapter in (a for a in candidates if a is not None):
            try:
                path = await asyncio.wait_for(
                    adapter.screenshot(ctx.evidence_path(action.action_id, "failure"), action.target),
                    timeout=_SCREENSHOT_TIMEOUT_S,
                )
            except Exception:
                logger.debug("failure screenshot via %s failed", adapter.name, exc_info=True)
                continue
            if path is not None:
                return [Evidence(type="screenshot", value=str(path), metadata={"reason": "failure", "adapter": adapter.name})]
        return []

    # ------------------------------------------------------------------ result

    def _finish(
        self,
        action_id: str,
        action: Action | None,
        started_at: Any,
        t0: float,
        *,
        output: Any = None,
        error: ActionError | None = None,
        retryable: bool = False,
        attempts: int,
        risk: Any = None,
        pre_checks: list[ConditionCheck] | None = None,
        outcome_checks: list[ConditionCheck] | None = None,
        evidence: list[Evidence] | None = None,
        adapter: str | None = None,
    ) -> ActionResult:
        result = ActionResult(
            success=error is None,
            action_id=action_id,
            action_type=action.type.value if action is not None else None,
            output=output,
            evidence=evidence or [],
            error=error.to_info(retryable=retryable) if error is not None else None,
            duration_ms=int((time.monotonic() - t0) * 1000),
            retryable=retryable if error is not None else False,
            attempts=attempts,
            adapter=adapter,
            risk=risk,
            precondition_checks=pre_checks or [],
            outcome_checks=outcome_checks or [],
            started_at=started_at,
            finished_at=utcnow(),
        )
        level = logging.INFO if result.success else logging.WARNING
        logger.log(
            level,
            "action.finish id=%s type=%s success=%s duration_ms=%d attempts=%d error=%s",
            result.action_id, result.action_type, result.success, result.duration_ms, result.attempts,
            result.error.error_type if result.error else None,
            extra={"action_event": {"event": "finish", **result.model_dump(mode="json", exclude={"output"})}},
        )
        return result


class SyncActionExecutor:
    """Synchronous facade for callers without an event loop (e.g. a Tk UI thread).

    Runs the executor on one private event-loop thread. Playwright objects are bound to the
    loop that created them, so every call must go through the same loop, which this guarantees.
    """

    def __init__(self, executor: ActionExecutor) -> None:
        self._executor = executor
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="action-executor", daemon=True)
        self._thread.start()

    def execute(self, action: Action | Mapping[str, Any], wait_timeout: float | None = None) -> ActionResult:
        future = asyncio.run_coroutine_threadsafe(self._executor.execute(action), self._loop)
        return future.result(wait_timeout)

    def warm_up(self) -> dict[str, str]:
        return asyncio.run_coroutine_threadsafe(self._executor.warm_up(), self._loop).result()

    def close(self) -> None:
        asyncio.run_coroutine_threadsafe(self._executor.close(), self._loop).result(30)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


def build_default_executor(settings: ActionSettings | None = None) -> ActionExecutor:
    """Executor wired with the real Playwright, Windows UI Automation and PyAutoGUI adapters.
    Libraries are imported lazily, so a missing one only disables its surface."""
    from actions.browser import PlaywrightAdapter
    from actions.screen import VisualAdapter
    from actions.windows import WindowsAdapter

    settings = settings or ActionSettings()
    return ActionExecutor(
        {"browser": PlaywrightAdapter(settings), "window": WindowsAdapter(settings), "screen": VisualAdapter(settings)},
        settings=settings,
    )

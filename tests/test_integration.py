"""End-to-end Memory + Action flow (spec sections 52-54), with the planner and verifier played
by the test. Uses fake adapters and the fake embedder: no browser, desktop or model needed."""

from __future__ import annotations

from pathlib import Path

import pytest

from actions.base import AdapterOutcome
from actions.config import ActionSettings
from actions.executor import ActionExecutor
from actions.models import Evidence
from memory.config import MemorySettings
from memory.manager import MemoryManager
from memory.workflow import WorkflowFreshness

from .actions.conftest import FakeAdapter
from .memory.conftest import FakeEmbedder

TASK = "Download my electricity bill"


@pytest.fixture
def system(tmp_path: Path):
    adapters = {"browser": FakeAdapter("browser"), "window": FakeAdapter("windows"), "screen": FakeAdapter("visual")}
    executor = ActionExecutor(adapters, settings=ActionSettings(
        evidence_dir=tmp_path / "ev", download_dir=tmp_path / "dl", browser_user_data_dir=None,
        screenshot_on_failure=False, _env_file=None))
    memory = MemoryManager(MemorySettings(db_path=tmp_path / "m.sqlite3", lancedb_path=tmp_path / "lance",
                                          _env_file=None), embedder=FakeEmbedder())
    yield executor, memory, adapters, tmp_path
    memory.close()


def planned_actions() -> list[dict]:
    return [
        {"type": "open_url", "parameters": {"url": "https://energy.example.in"}},
        {"type": "click", "target": {"surface": "browser", "role": "link", "name": "Bills"}},
        {"type": "download", "target": {"surface": "browser", "role": "button", "name": "Download"},
         "expected_outcomes": [{"kind": "download_completed", "description": "PDF downloaded"}]},
    ]


def script_download(adapters, pdf: Path, ok: bool = True) -> None:
    if ok:
        pdf.write_bytes(b"%PDF-1.4")
    adapters["browser"].script = [
        AdapterOutcome(output={"url": "https://energy.example.in"}),
        AdapterOutcome(output={"matches": 1}),
        AdapterOutcome(output={"path": str(pdf)}, evidence=[Evidence(type="download_path", value=str(pdf))]),
    ]


async def run(executor, memory, payloads):
    results = []
    for payload in payloads:
        memory.working.add_pending(payload)
        result = await executor.execute(payload)
        memory.working.record_action_result(result)
        results.append(result)
        if not result.success:
            break
    return results


async def test_first_then_subsequent_execution(system):
    executor, memory, adapters, tmp = system

    # ---- first execution (spec 53): no workflow yet, planner builds actions itself
    memory.working.set_task(TASK)
    assert memory.get_workflow(TASK) is None
    script_download(adapters, tmp / "bill.pdf")
    results = await run(executor, memory, planned_actions())
    assert all(r.success for r in results)

    # higher-level verifier: execution success AND the expected outcome was observed
    verified = results[-1].expected_outcomes_met is True
    assert verified
    memory.save({"type": "episode", "source": "execution", "content": "Downloaded the electricity bill PDF",
                 "metadata": {"execution_id": "exec_1"}})
    workflow = memory.save_workflow({
        "goal": "Download electricity bill",
        "preconditions": [{"description": "Browser available"}],
        "steps": [{"order": i + 1, "description": p["type"], "action_type": p["type"], "target": p.get("target"),
                   "parameters": p.get("parameters", {}),
                   "expected_conditions": [{"description": c["description"], "kind": c["kind"]}
                                           for c in p.get("expected_outcomes", [])]}
                  for i, p in enumerate(planned_actions())],
        "expected_outcomes": [{"description": "PDF exists in the download folder"}],
        "verification_history": [{"execution_id": "exec_1", "success": True,
                                  "evidence": [e.model_dump(mode="json") for e in results[-1].evidence]}],
    })

    # ---- subsequent execution (spec 54): workflow found, freshness checked, then replayed via executor
    memory.working.set_task(TASK)
    match = memory.get_workflow(TASK)
    assert match is not None and match.is_current and match.workflow.id == workflow.id
    ctx = memory.build_context(TASK)
    assert "Download electricity bill" in ctx.to_prompt()

    script_download(adapters, tmp / "bill2.pdf")
    results = await run(executor, memory, match.workflow.action_payloads())
    assert all(r.success for r in results) and results[-1].expected_outcomes_met
    updated = memory.record_workflow_verification(workflow.id, {"execution_id": "exec_2", "success": True})
    assert updated.success_count == 2


async def test_execution_success_without_task_success_does_not_create_workflow(system):
    """Rule 11 + spec 26: click works, file never appears -> episode, not workflow."""
    executor, memory, adapters, tmp = system
    adapters["browser"].script = [AdapterOutcome(), AdapterOutcome(), AdapterOutcome(output={"path": None})]
    results = await run(executor, memory, planned_actions())
    assert all(r.success for r in results)
    assert results[-1].expected_outcomes_met is False  # verifier sees the task failed

    memory.save({"type": "episode", "source": "execution", "content": "Bill download attempt failed: no file appeared"})
    with pytest.raises(ValueError):
        memory.save_workflow({"goal": "Download electricity bill",
                              "steps": [{"order": 1, "description": "x", "action_type": "click"}],
                              "verification_history": [{"execution_id": "exec_x", "success": False}]})
    assert memory.get_workflow(TASK) is None
    assert memory.working.last_action_result["action_type"] == "download"


async def test_failed_replay_marks_workflow_failing(system):
    executor, memory, adapters, tmp = system
    wf = memory.save_workflow({
        "goal": "Download electricity bill",
        "steps": [{"order": 1, "description": "Click download", "action_type": "click",
                   "target": {"surface": "browser", "role": "button", "name": "Download"}}],
        "verification_history": [{"execution_id": "exec_1", "success": True}],
    })
    from actions.errors import TargetNotFoundError

    adapters["browser"].script = [TargetNotFoundError("button renamed")] * 3
    results = await run(executor, memory, memory.get_workflow(TASK).workflow.action_payloads())
    assert results[-1].error.error_type == "TargetNotFoundError"
    memory.record_workflow_verification(wf.id, {"execution_id": "exec_2", "success": False,
                                                "observed_changes": [results[-1].error.message]})
    match = memory.get_workflow(TASK)
    assert match.freshness is WorkflowFreshness.FAILING and "Adapt" in match.caveat

from datetime import timedelta

import pytest

from memory.models import WorkflowNotVerifiedError, utcnow
from memory.workflow import Workflow, WorkflowFreshness, WorkflowVerification

NOW = utcnow()


def bill_workflow(**kw):
    return {
        "goal": "Download electricity bill",
        "description": "Get the latest bill PDF from the JVVNL portal",
        "preconditions": [
            {"description": "Browser available"},
            {"description": "User is authenticated", "kind": "element_visible",
             "target": {"surface": "browser", "role": "link", "name": "Logout"}},
            {"description": "Bills page reachable", "kind": "url_contains", "value": "energy"},
        ],
        "steps": [
            {"order": 2, "description": "Navigate to billing section", "action_type": "click",
             "target": {"surface": "browser", "role": "link", "name": "Bills"}},
            {"order": 1, "description": "Open provider website", "action_type": "open_url",
             "parameters": {"url": "https://energy.example.in"}},
            {"order": 3, "description": "Click download", "action_type": "download",
             "target": {"surface": "browser", "role": "button", "name": "Download"},
             "expected_outcome": "A PDF download begins",
             "expected_conditions": [{"description": "download finished", "kind": "download_completed"}]},
        ],
        "expected_outcomes": [{"description": "PDF exists in the download folder"}],
        "failure_conditions": [{"description": "Session expired page shown", "kind": "text_present", "value": "Session expired"}],
        "verification_history": [{"execution_id": "exec_1", "success": True, "timestamp": NOW,
                                  "evidence": [{"type": "download_path", "value": "C:/dl/bill.pdf"}]}],
        "metadata": {"application": "browser", "domain": "energy.example.in", "tags": ["bill", "electricity"]},
        **kw,
    }


def test_steps_are_ordered_and_unique():
    w = Workflow.model_validate(bill_workflow())
    assert [s.order for s in w.steps] == [1, 2, 3]
    with pytest.raises(ValueError):
        Workflow.model_validate(bill_workflow(steps=[
            {"order": 1, "description": "a", "action_type": "click"},
            {"order": 1, "description": "b", "action_type": "click"},
        ]))


def test_workflow_needs_verified_success_before_storage(manager):
    with pytest.raises(WorkflowNotVerifiedError):
        manager.save_workflow(bill_workflow(verification_history=[]))
    with pytest.raises(WorkflowNotVerifiedError):
        manager.save_workflow(bill_workflow(verification_history=[{"execution_id": "e", "success": False}]))
    w = manager.save_workflow(bill_workflow())
    assert w.last_verified_at == NOW
    assert w.source_execution_id == "exec_1"
    assert w.verification_history[0].workflow_version == 1


def test_workflows_are_not_generic_memories(manager):
    manager.save_workflow(bill_workflow())
    assert manager.list_memories() == []
    assert manager.search("electricity bill") == []
    assert manager.get_workflow("electricity bill download") is not None


def test_get_workflow_by_task_in_any_language(manager):
    w = manager.save_workflow(bill_workflow())
    for task in ("download electricity bill", "बिजली का बिल डाउनलोड", "bijli bill"):
        match = manager.get_workflow(task)
        assert match is not None and match.workflow.id == w.id, task
        assert match.is_current
    assert manager.get_workflow("play music on youtube") is None


def test_freshness_states(manager, settings):
    stale_after = timedelta(days=settings.workflow_stale_after_days)
    w = Workflow.model_validate(bill_workflow())
    w.last_verified_at = NOW
    assert w.freshness(NOW, stale_after) is WorkflowFreshness.CURRENT
    assert w.freshness(NOW + timedelta(days=200), stale_after) is WorkflowFreshness.STALE  # "worked six months ago"
    expired = w.model_copy(update={"expires_at": NOW - timedelta(days=1)})
    assert expired.freshness(NOW, stale_after) is WorkflowFreshness.EXPIRED
    failing = w.model_copy(update={"verification_history": [
        *w.verification_history, WorkflowVerification(execution_id="e2", success=False, timestamp=NOW + timedelta(minutes=1))]})
    assert failing.freshness(NOW, stale_after) is WorkflowFreshness.FAILING


def test_stale_workflow_is_returned_with_caveat(manager):
    old = NOW - timedelta(days=180)
    manager.save_workflow(bill_workflow(verification_history=[{"execution_id": "e", "success": True, "timestamp": old}]))
    match = manager.get_workflow("download electricity bill")
    assert match.freshness is WorkflowFreshness.STALE and not match.is_current
    assert "Revalidate" in match.caveat
    assert manager.get_workflow("download electricity bill", current_only=True) is None


def test_verification_history_updates(manager):
    w = manager.save_workflow(bill_workflow())
    failed = manager.record_workflow_verification(w.id, {"execution_id": "exec_2", "success": False,
                                                         "observed_changes": ["Download button renamed"]})
    assert failed.confidence < w.confidence
    assert failed.last_verified_at == w.last_verified_at
    assert manager.get_workflow("download electricity bill").freshness is WorkflowFreshness.FAILING

    ok = manager.record_workflow_verification(w.id, WorkflowVerification(execution_id="exec_3", success=True))
    assert ok.last_verified_at > w.last_verified_at
    assert ok.confidence > failed.confidence
    assert [v.execution_id for v in ok.verification_history] == ["exec_1", "exec_2", "exec_3"]
    assert ok.success_count == 2 and ok.failure_count == 1


def test_procedural_update_creates_new_unverified_version(manager):
    w = manager.save_workflow(bill_workflow())
    new_steps = [s.model_dump() for s in w.steps]
    new_steps[2]["target"] = {"surface": "browser", "role": "button", "name": "Download PDF"}
    v2 = manager.update_workflow(w.id, {"steps": new_steps})
    assert v2.version == 2 and v2.last_verified_at is None
    assert manager.get_workflow("download electricity bill").freshness is WorkflowFreshness.UNVERIFIED
    assert [old.version for old in manager.workflow_versions(w.id)] == [1]
    assert manager.workflow_versions(w.id)[0].steps[2].target["name"] == "Download"

    v2_ok = manager.record_workflow_verification(w.id, {"execution_id": "exec_9", "success": True})
    assert v2_ok.verification_history[-1].workflow_version == 2
    assert manager.get_workflow("download electricity bill").is_current


def test_metadata_update_does_not_bump_version(manager):
    w = manager.save_workflow(bill_workflow())
    updated = manager.update_workflow(w.id, {"metadata": {"tags": ["bill", "jvvnl"]}, "confidence": 0.7})
    assert updated.version == 1 and updated.last_verified_at == w.last_verified_at
    assert updated.metadata["application"] == "browser" and "jvvnl" in updated.metadata["tags"]


def test_update_with_verification_in_one_call(manager):
    w = manager.save_workflow(bill_workflow())
    updated = manager.update_workflow(
        w.id, {"failure_conditions": []},
        verification=WorkflowVerification(execution_id="exec_5", success=True),
    )
    assert updated.version == 2 and updated.last_verified_at is not None
    assert manager.get_workflow("electricity bill").is_current


def test_sensitive_step_inputs_are_not_stored(manager):
    data = bill_workflow()
    data["steps"].append({"order": 4, "description": "Enter password", "action_type": "type",
                          "target": {"surface": "browser", "label": "Password"},
                          "parameters": {"text": "Sup3r$ecret", "sensitive": True}})
    data["steps"].append({"order": 5, "description": "Enter OTP", "action_type": "type",
                          "parameters": {"text": "OTP 482913"}})
    w = manager.save_workflow(data)
    stored = manager.get_workflow_by_id(w.id)
    assert "Sup3r$ecret" not in stored.model_dump_json() and "482913" not in stored.model_dump_json()
    assert stored.steps[3].requires_user_input
    assert set(stored.metadata["redactions"]) >= {"sensitive_input", "otp"}


def test_invalidate_and_forget_workflow(manager):
    w = manager.save_workflow(bill_workflow())
    manager.invalidate_workflow(w.id, reason="portal redesigned")
    assert manager.get_workflow("electricity bill") is None
    assert manager.find_workflows("electricity bill", include_invalid=True)[0].freshness is WorkflowFreshness.INVALID
    assert manager.forget_workflow(w.id)
    assert manager.get_workflow_by_id(w.id) is None


def test_steps_are_action_block_compatible(manager):
    """Steps round-trip into valid actions.Action objects without Memory importing Action."""
    from actions.models import Action
    from actions.registry import ActionRegistry

    w = manager.save_workflow(bill_workflow())
    registry = ActionRegistry()
    actions = [Action.model_validate(p) for p in w.action_payloads()]
    for action in actions:
        registry.check(action)
    assert [a.type.value for a in actions] == ["open_url", "click", "download"]
    assert actions[2].expected_outcomes[0].kind == "download_completed"
    preconditions = [c.to_action_condition() for c in w.preconditions]
    from actions.models import Condition
    assert [Condition.model_validate(c).kind for c in preconditions] == ["custom", "element_visible", "url_contains"]

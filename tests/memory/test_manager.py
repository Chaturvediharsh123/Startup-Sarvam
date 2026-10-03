import ast
from datetime import timedelta
from pathlib import Path

import pytest

from memory.manager import MemoryManager
from memory.models import Freshness, MemoryNotFoundError, MemoryStatus, utcnow
from memory.working import WorkingMemory

from .conftest import FakeEmbedder

MEMORY_DIR = Path(__file__).resolve().parents[2] / "memory"


def test_save_get_update_forget(manager):
    m = manager.save({"type": "fact", "content": {"provider": "JVVNL"}, "source": "agent",
                      "metadata": {"tags": ["electricity"], "domain": "energy"}})
    assert manager.get(m.id) == m
    updated = manager.update(m.id, {"confidence": 0.7, "metadata": {"application": "browser"}})
    assert updated.confidence == 0.7
    assert updated.metadata.tags == ["electricity"] and updated.metadata.application == "browser"
    assert updated.updated_at >= m.updated_at and updated.created_at == m.created_at
    assert manager.forget(m.id) is True
    assert manager.get(m.id) is None and manager.search("JVVNL") == []
    assert manager.forget(m.id) is False
    with pytest.raises(MemoryNotFoundError):
        manager.update(m.id, {"confidence": 0.1})


def test_user_statements_are_verified_inferences_are_not(manager):
    stated = manager.save({"type": "preference", "content": "Replies in Hindi", "source": "user"})
    inferred = manager.save({"type": "preference", "content": "Probably prefers mornings", "source": "agent"})
    assert stated.last_verified_at is not None and stated.metadata.verification_method == "user_statement"
    assert [v.method for v in manager.verification_history(stated.id)] == ["user_statement"]
    assert inferred.last_verified_at is None
    assert inferred.confidence < stated.confidence


def test_verify_records_method_and_evidence(manager):
    m = manager.save({"type": "observation", "content": "Bills page has a Download PDF button", "source": "execution"})
    verified = manager.verify(m.id, method="successful_execution",
                              evidence=[{"type": "screenshot", "value": "ev/1.png"}, {"type": "otp", "value": "OTP 123456"}])
    assert verified.last_verified_at is not None
    assert verified.freshness() is Freshness.VERIFIED
    history = manager.verification_history(m.id)
    assert history[0].method == "successful_execution"
    assert "123456" not in str(history[0].evidence)


def test_content_change_clears_verification(manager):
    m = manager.save({"type": "fact", "content": "Bill due on the 10th", "source": "user"})
    changed = manager.update(m.id, {"content": "Bill due on the 15th"})
    assert changed.last_verified_at is None
    same = manager.update(changed.id, {"content": "Bill due on the 15th", "confidence": 0.95})
    assert same.last_verified_at is None


def test_expiry_and_invalidate(manager):
    m = manager.save({"type": "observation", "content": "Portal down", "source": "execution",
                      "expires_at": utcnow() + timedelta(hours=1)})
    assert m.freshness() is Freshness.UNVERIFIED
    assert m.freshness(utcnow() + timedelta(hours=2)) is Freshness.EXPIRED
    inv = manager.invalidate(m.id, reason="came back up")
    assert inv.status is MemoryStatus.INVALID and inv.metadata.invalidated_reason == "came back up"


def test_sensitive_data_never_reaches_storage(manager, settings):
    m = manager.save({"type": "episode", "source": "execution",
                      "content": "Logged in with password: Hunter2! and OTP 482913",
                      "metadata": {"session_id": "abc", "source_reference": "Cookie: sid=zzz"}})
    assert "Hunter2!" not in str(m.content) and "482913" not in str(m.content)
    assert set(m.metadata.redactions) >= {"password", "otp", "sensitive_field", "session_cookie"}
    raw = Path(settings.db_path).read_bytes()
    assert b"Hunter2!" not in raw and b"482913" not in raw and b"zzz" not in raw


def test_update_is_filtered_too(manager):
    m = manager.save({"type": "fact", "content": "UPI app is BHIM", "source": "user"})
    updated = manager.update(m.id, {"content": "UPI app is BHIM, UPI PIN 4321"})
    assert "4321" not in updated.content and "pin" in updated.metadata.redactions


def test_data_survives_restart(settings):
    with MemoryManager(settings, embedder=FakeEmbedder()) as first:
        m = first.save({"type": "fact", "content": "Electricity provider is JVVNL", "source": "user"})
    with MemoryManager(settings, embedder=FakeEmbedder()) as second:
        assert second.get(m.id).content == "Electricity provider is JVVNL"
        assert second.search("बिजली")[0].memory.id == m.id
        assert second.working.snapshot().current_task is None  # working memory is RAM-only


def test_keyword_only_mode_without_semantic_dependencies(keyword_manager):
    assert not keyword_manager.semantic_enabled
    keyword_manager.save({"type": "fact", "content": "Electricity provider is JVVNL", "source": "user"})
    assert keyword_manager.search("electricity provider")


def test_other_components_cannot_reach_storage_through_public_api():
    public = {n for n in dir(MemoryManager) if not n.startswith("_")}
    assert not {"store", "semantic", "conn", "connection", "db"} & public


def test_memory_block_never_imports_action_or_planner_code():
    for path in MEMORY_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert name.split(".")[0] not in {"actions", "planner", "playwright", "pywinauto", "pyautogui"}, path.name


# --------------------------------------------------------------------------- working memory

def test_working_memory_tracks_current_task():
    wm = WorkingMemory(max_items=3)
    wm.set_task("open notepad")
    wm.set_application("notepad")
    wm.set_page("https://x.test", "X")
    wm.update_state(step=2)
    wm.add_turn("user", "Notepad kholo")
    wm.add_turn("user", "ab isko band karo")
    wm.add_observation("Notepad window visible")
    wm.add_pending({"action_id": "act_1", "type": "close_app"})
    wm.record_action_result({"action_id": "act_1", "action_type": "close_app", "success": True, "output": {"closed": True},
                             "evidence": [{"type": "window_title", "value": "Untitled - Notepad"}], "error": None})
    snap = wm.snapshot()
    assert snap.current_task == "open notepad" and snap.current_application == "notepad"
    assert snap.task_state == {"step": 2}
    assert [t["text"] for t in snap.recent_turns] == ["Notepad kholo", "ab isko band karo"]
    assert snap.pending_actions == []  # completed action removed from pending
    assert wm.last_action_result["evidence"][0]["value"] == "Untitled - Notepad"


def test_working_memory_is_bounded_and_resets_per_task():
    wm = WorkingMemory(max_items=2)
    for i in range(5):
        wm.add_observation(f"obs {i}")
        wm.add_turn("user", f"turn {i}")
    snap = wm.snapshot()
    assert [o["text"] for o in snap.recent_observations] == ["obs 3", "obs 4"]
    wm.set_task("new task")
    snap = wm.snapshot()
    assert snap.recent_observations == [] and len(snap.recent_turns) == 2  # turns kept for follow-ups
    wm.clear(keep_turns=False)
    assert wm.snapshot().recent_turns == []


def test_working_memory_accepts_action_result_models():
    from actions.models import ActionResult

    now = utcnow()
    result = ActionResult(success=False, action_id="act_9", action_type="click", duration_ms=5,
                          started_at=now, finished_at=now, output={"text": "x" * 2000})
    summary = WorkingMemory().record_action_result(result)
    assert summary["action_id"] == "act_9" and summary["success"] is False
    assert len(summary["output"]["text"]) < 600

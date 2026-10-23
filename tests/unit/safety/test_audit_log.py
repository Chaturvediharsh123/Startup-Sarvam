"""Audit log: one JSON line per request / decision / result, secrets never written."""

from __future__ import annotations

import json
from pathlib import Path

from core.config import Settings
from core.events import ActionResult, BrainResult, SafetyDecision, Status, ToolCall, Verdict
from safety.audit_log import AuditLog


def lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_writes_request_decision_result(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "logs" / "audit.jsonl")
    call = ToolCall("open_app", {"name": "crome"})
    log.handle(BrainResult("Opening", "hi-IN", tool_call=call, turn_id="t1"))
    log.handle(SafetyDecision(Verdict.ALLOW, call, reason="low risk",
                              clean_args={"name": "chrome"}, turn_id="t1"))
    log.handle(ActionResult("open_app", {"name": "chrome"}, "ok", "opened chrome",
                            duration_ms=12, fact="opened chrome", turn_id="t1"))
    log.handle(Status("ignored"))
    log.handle(BrainResult("just chat", "hi-IN", turn_id="t1"))  # no tool call: ignored
    entries = lines(log.path)
    assert [e["kind"] for e in entries] == ["request", "decision", "result"]
    assert all(e["turn_id"] == "t1" and e["time"] for e in entries)
    assert entries[1]["verdict"] == "allow" and entries[1]["args"] == {"name": "chrome"}
    assert entries[2]["status"] == "ok" and entries[2]["fact"] == "opened chrome"
    assert log.recent(2) == entries[1:]


def test_truncates_long_text_and_keeps_unicode(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "a.jsonl", max_text=20)
    log.handle(ActionResult("type_text", {"text": "नमस्ते " * 50}, "ok", "x", fact="typed"))
    entry = lines(log.path)[0]
    assert entry["args"]["text"].startswith("नमस्ते")
    assert "...(+" in entry["args"]["text"]
    assert "नमस्ते" in log.path.read_text(encoding="utf-8")


def test_secrets_never_logged(tmp_path: Path, settings: Settings) -> None:
    log = AuditLog.from_settings(settings)
    log.handle(ActionResult(
        "type_text",
        {"text": f"my key is {settings.sarvam_api_key} and sk_live_ABCDEFGH12345",
         "api_key": "anything", "password": "hunter2"},
        "ok", "x", fact="typed",
    ))
    raw = log.path.read_text(encoding="utf-8")
    assert settings.sarvam_api_key not in raw
    assert "sk_live_ABCDEFGH12345" not in raw
    assert "hunter2" not in raw and "anything" not in raw
    assert log.path == settings.log_dir / "audit.jsonl"


def test_recent_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    first = AuditLog(path)
    for i in range(5):
        first.record({"kind": "result", "tool": f"t{i}"})
    second = AuditLog(path)
    assert [e["tool"] for e in second.recent(3)] == ["t2", "t3", "t4"]
    assert second.recent(0) == []


def test_rotation_and_write_failure_never_raise(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "a.jsonl", max_bytes=100)
    for i in range(10):
        log.record({"kind": "result", "tool": "x" * 30, "i": i})
    assert (tmp_path / "a.jsonl.1").is_file()
    blocked = tmp_path / "file"
    blocked.write_text("x")
    broken = AuditLog(blocked / "audit.jsonl")  # parent is a file: cannot write
    broken.handle(ActionResult("x", {}, "ok", "x"))
    assert broken.recent(1)[0]["tool"] == "x"

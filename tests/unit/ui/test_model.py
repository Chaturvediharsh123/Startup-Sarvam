"""Event -> window state mapping and format helpers (pure, no display needed)."""

from __future__ import annotations

from pathlib import Path

from core.config import Settings, save_env_values
from core.events import (
    ActionResult,
    BrainResult,
    ErrorRaised,
    FinalTranscript,
    HealthChanged,
    Interrupt,
    LanguageDetected,
    Metrics,
    MicLevel,
    ModeChanged,
    PartialTranscript,
    SafetyDecision,
    SpeakRequest,
    StateChanged,
    Status,
    ToolCall,
    TurnState,
    Verdict,
)
from ui.app import apply_settings_values, format_action, format_timing
from ui.widgets import (
    ACTIVITY,
    ACTIVITY_LIMIT,
    AUDIT,
    LEVEL,
    REPLY,
    SPEED,
    STATUS,
    TRANSCRIPT,
    UiModel,
    audit_tag,
    clock,
    decision_entry,
    format_activity,
    format_audit,
    metric_values,
    state_text,
    status_word,
)


def _result(name: str = "open_app", status: str = "ok", fact: str = "opened notepad",
            result: object = "Opened notepad", ts: float = 0.0) -> ActionResult:
    return ActionResult(name, {"name": "notepad"}, status, result, fact=fact, ts=ts)


def test_format_timing_accepts_metrics_and_dicts() -> None:
    metrics = Metrics(stt_ms=400, llm_ms=900, tts_first_ms=300, total_ms=1600)
    expected = "STT 400 ms · LLM 900 ms · first audio 300 ms · total 1600 ms"
    assert format_timing(metrics) == expected
    assert format_timing({"stt_ms": 400, "llm_ms": 900, "action_ms": 0, "tts_first_ms": 300,
                          "total_ms": 1600}) == expected
    assert format_timing({"llm_ms": 900, "action_ms": 120}) == "LLM 900 ms · action 120 ms"
    assert format_timing(None) == ""
    assert format_timing({"total_ms": 1200}, "hi") == "कुल 1200 मि.से."


def test_format_action() -> None:
    assert format_action(_result()) == "✓  open_app(name=notepad) — opened notepad"
    denied = ActionResult("shutdown_pc", {}, "denied", "user did not confirm")
    assert format_action(denied).startswith("✗ denied")
    assert format_action(denied).endswith("— user did not confirm")
    info = ActionResult("system_status", {}, "ok", {"battery_percent": 80})
    assert format_action(info).endswith("— info")


def test_state_and_status_words() -> None:
    assert state_text(TurnState.LISTENING) == "Listening…"
    assert state_text(TurnState.AWAITING_YES, "hi") == "आपकी हाँ का इंतज़ार"
    assert state_text("speaking") == "Speaking…"
    assert status_word("ok") == "done"
    assert status_word("deny", "hi") == "मना किया"
    assert status_word("weird") == "weird"


def test_clock() -> None:
    assert clock("2026-10-08T14:02:05.123") == "14:02:05"
    assert clock("2026-10-08 14:02:05") == "14:02:05"
    assert len(clock(1_700_000_000.0)) == 8
    assert clock(None) == ""


def test_transcript_partial_is_replaced_by_final() -> None:
    model = UiModel()
    assert model.apply(PartialTranscript("नोटपैड")) == {TRANSCRIPT}
    assert model.partial == "नोटपैड"
    changed = model.apply(FinalTranscript("नोटपैड खोलो", "hi-IN"))
    assert TRANSCRIPT in changed and REPLY in changed
    assert model.partial == ""
    assert list(model.finals) == ["नोटपैड खोलो"]
    assert model.language == "hi-IN"


def test_reply_said_comes_from_speak_requests_and_did_from_results() -> None:
    model = UiModel()
    model.apply(FinalTranscript("open notepad"))
    model.apply(BrainResult("draft reply", "en-IN", tool_call=ToolCall("open_app")))
    assert model.said == ""  # the brain's draft is not what was spoken
    model.apply(SpeakRequest("Opening Notepad."))
    assert model.said == "Opening Notepad."
    assert model.apply(_result()) == {ACTIVITY, REPLY}
    assert model.did == "open_app: opened notepad"
    assert model.did_status == "ok"
    model.apply(FinalTranscript("next"))
    assert (model.said, model.did) == ("", "")


def test_safety_decisions_fill_the_audit_including_denials() -> None:
    model = UiModel()
    deny = SafetyDecision(Verdict.DENY, ToolCall("shutdown_pc"), reason="not allowed now",
                          turn_id="t1")
    assert model.apply(deny) == {AUDIT, REPLY}
    assert model.did_status == "denied"
    assert "not allowed now" in model.did
    confirm = SafetyDecision(Verdict.CONFIRM, ToolCall("delete_file"), question="Delete it?")
    assert model.apply(confirm) == {AUDIT}
    rows = list(model.audit)
    assert rows[0]["verdict_or_status"] == "deny"
    assert rows[0]["reason_or_fact"] == "not allowed now"
    assert rows[0]["turn_id"] == "t1"
    assert rows[1]["reason_or_fact"] == "Delete it?"
    assert audit_tag(rows[0]) == "bad" and audit_tag(rows[1]) == "warn"
    assert format_audit(rows[0]).endswith("shutdown_pc — denied: not allowed now")


def test_decision_entry_keys_match_audit_store() -> None:
    entry = decision_entry(SafetyDecision(Verdict.ALLOW, ToolCall("set_volume")))
    assert set(entry) == {"time", "turn_id", "kind", "tool", "verdict_or_status",
                          "reason_or_fact"}
    assert entry["kind"] == "decision"


def test_set_audit_sorts_by_time() -> None:
    model = UiModel()
    model.set_audit([{"time": 2.0, "tool": "b"}, {"time": 1.0, "tool": "a"}])
    assert [row["tool"] for row in model.audit] == ["a", "b"]
    model.set_audit([{"time": "x", "tool": "s"}, {"time": 1.0, "tool": "n"}])  # mixed: unsorted
    assert [row["tool"] for row in model.audit] == ["s", "n"]


def test_activity_keeps_last_20() -> None:
    model = UiModel()
    for i in range(ACTIVITY_LIMIT + 5):
        model.apply(_result(name=f"a{i}"))
    assert len(model.activity) == ACTIVITY_LIMIT
    assert model.activity[0].name == "a5"
    line = format_activity(_result(status="error", fact="", result="boom"))
    assert line.endswith("✗ open_app — error: boom")


def test_status_health_language_and_errors() -> None:
    model = UiModel()
    assert model.health["stt"] is None
    model.apply(StateChanged(TurnState.IDLE, TurnState.LISTENING))
    assert model.state == TurnState.LISTENING
    model.apply(HealthChanged("stt", False, "socket closed"))
    assert model.health["stt"] is False
    assert model.health_detail["stt"] == "socket closed"
    model.apply(HealthChanged("camera", True))
    assert model.health["camera"] is True
    model.apply(LanguageDetected("ta-IN"))
    assert model.language == "ta-IN"
    model.apply(Status("waiting for yes/no"))
    assert (model.detail_kind, model.detail) == ("status", "waiting for yes/no")
    assert model.apply(ErrorRaised("mic busy", "audio")) == {STATUS}
    assert model.detail == "audio: mic busy"
    model.apply(FinalTranscript("hello"))
    assert model.detail_kind == ""  # a new turn clears the old error
    model.apply(PartialTranscript("half"))
    model.apply(Interrupt("user"))
    assert model.detail_kind == "stopped" and model.partial == ""


def test_metrics_level_and_mode() -> None:
    model = UiModel()
    assert model.apply(Metrics(stt_ms=1, total_ms=5)) == {SPEED}
    assert metric_values(model.metrics)["total_ms"] == 5
    assert model.apply(MicLevel(1.7)) == {LEVEL}
    assert model.level == 1.0
    model.apply(ModeChanged(live=True, language_hint="hi-IN"))
    assert model.live is True and model.language_hint == "hi-IN"
    model.apply(ModeChanged(language_hint=""))
    assert model.language_hint is None
    assert model.apply(object()) == frozenset()


def test_apply_settings_values(settings: Settings) -> None:
    new = apply_settings_values(settings, {
        "UI_FONT_SIZE": "26", "UI_THEME": "high_contrast", "UI_LANGUAGE": "hi",
        "TTS_SPEAKER": "ishita", "WAKE_WORD": "sarvam", "LANGUAGE_HINT": "ta-IN",
        "MIC_DEVICE": "USB Mic", "SPEAKER_DEVICE": "Speakers",  # not a Settings field yet
    })
    assert new.ui_font_size == 26
    assert new.ui_theme == "high_contrast"
    assert new.ui_language == "hi"
    assert new.tts_speaker == "ishita"
    assert new.wake_word == "sarvam"
    assert new.language_hint == "ta-IN"
    assert new.mic_device == "USB Mic"
    assert settings.ui_theme == "dark"  # the original is unchanged


def test_save_env_values_keeps_other_lines(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# comment\nSARVAM_API_KEY=sk_secret\nTTS_SPEAKER=shubh\n", encoding="utf-8")
    save_env_values({"TTS_SPEAKER": "priya", "WAKE_WORD": "sarvam"}, env)
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "# comment", "SARVAM_API_KEY=sk_secret", "TTS_SPEAKER=priya", "WAKE_WORD=sarvam"
    ]

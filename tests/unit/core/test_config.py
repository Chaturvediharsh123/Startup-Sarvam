"""Tests for settings loading and logging setup."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from core.config import PROJECT_ROOT, ConfigError, Settings, load_settings
from core.logging import ACTIONS_LOGGER, setup_logging, shutdown_logging


def _load(**overrides: str) -> Settings:
    values = {"SARVAM_API_KEY": "sk_abc"}
    values.update(overrides)
    return load_settings(env_file=None, overrides=values)


def test_missing_key_gives_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="SARVAM_API_KEY"):
        load_settings(env_file=None, overrides={})


def test_placeholder_key_rejected() -> None:
    with pytest.raises(ConfigError):
        _load(SARVAM_API_KEY="sk_your_key_here")


def test_defaults() -> None:
    s = _load()
    assert s.llm_model == "sarvam-105b"
    assert s.stt_realtime_model == "saaras:v3-realtime"
    assert s.stt_model == "saaras:v4"
    assert s.tts_model == "bulbul:v3"
    assert s.sample_rate == 16000
    assert s.chunk_ms == 100
    assert s.chunk_samples == 1600
    assert s.history_turns == 4
    assert s.confirm_timeout_s == 10.0
    assert s.wake_word == ""
    assert s.db_path == PROJECT_ROOT / "data" / "assistant.db"


def test_settings_are_frozen() -> None:
    s = _load()
    with pytest.raises(AttributeError):
        s.llm_model = "x"  # type: ignore[misc]


def test_env_file_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)
    monkeypatch.delenv("WAKE_WORD", raising=False)
    env = tmp_path / ".env"
    env.write_text("SARVAM_API_KEY=sk_file\nWAKE_WORD=sarvam\n", encoding="utf-8")
    s = load_settings(env_file=env)
    assert s.sarvam_api_key == "sk_file"
    assert s.wake_word == "sarvam"


def test_relative_paths_resolve_to_project() -> None:
    s = _load(DB_PATH="data/x.db", LOG_DIR="mylogs")
    assert s.db_path == PROJECT_ROOT / "data" / "x.db"
    assert s.log_dir == PROJECT_ROOT / "mylogs"


@pytest.mark.parametrize(
    "key, value",
    [
        ("SAMPLE_RATE", "44100"),
        ("SAMPLE_RATE", "12000"),
        ("HISTORY_TURNS", "abc"),
        ("TTS_PACE", "5"),
        ("BARGE_IN", "maybe"),
        ("SARVAM_LLM_REASONING", "huge"),
    ],
)
def test_invalid_values_rejected(key: str, value: str) -> None:
    with pytest.raises(ConfigError):
        _load(**{key: value})


def test_bool_and_numbers_parse() -> None:
    s = _load(BARGE_IN="yes", CONFIRM_TIMEOUT_S="4.5", SAMPLE_RATE="8000")
    assert s.barge_in is True
    assert s.confirm_timeout_s == 4.5
    assert s.sample_rate == 8000


def test_logging_writes_both_files(tmp_path: Path) -> None:
    setup_logging(tmp_path, console=False)
    try:
        logging.getLogger("assistant.test").info("hello नमस्ते")
        logging.getLogger(ACTIONS_LOGGER).info("open_app ok")
    finally:
        shutdown_logging()
    app_log = (tmp_path / "app.log").read_text(encoding="utf-8")
    actions_log = (tmp_path / "actions.log").read_text(encoding="utf-8")
    assert "hello नमस्ते" in app_log
    assert "open_app ok" in actions_log
    assert "hello" not in actions_log


def test_logging_setup_twice_does_not_duplicate(tmp_path: Path) -> None:
    setup_logging(tmp_path, console=False)
    setup_logging(tmp_path, console=False)
    try:
        logging.getLogger("assistant.test").info("once")
    finally:
        shutdown_logging()
    assert (tmp_path / "app.log").read_text(encoding="utf-8").count("once") == 1

"""Tests for the Deepgram / Cartesia provider settings, key checks and wiring (ADR 0009)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import main
from core.config import ConfigError, Settings, check_speech_keys, load_settings
from core.logging import mask_secrets
from safety.audit_log import AuditLog
from sarvam.client import SarvamClient
from stt.batch_client import RestSTT
from stt.deepgram_batch import DeepgramBatchSTT
from tts.cartesia_client import CartesiaTTS
from tts.stream_client import SarvamTTS

DG_KEY = "0123456789abcdef0123456789abcdef01234567"
CA_KEY = "sk_car_test_0123456789"
VOICE = "00a472d9-32a2-4648-a733-f53e484a381e"


@pytest.fixture(autouse=True)
def _no_real_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keys in the developer's environment must not leak into these tests."""
    for name in ("DEEPGRAM_API_KEY", "CARTESIA_API_KEY", "CARTESIA_VOICE_ID",
                 "STT_PROVIDER", "TTS_PROVIDER", "DEEPGRAM_LANGUAGE"):
        monkeypatch.delenv(name, raising=False)


def _load(**overrides: str) -> Settings:
    return load_settings(env_file=None, overrides={"SARVAM_API_KEY": "sk_abc", **overrides},
                         settings_file=None)


def _full(**overrides: str) -> Settings:
    return _load(DEEPGRAM_API_KEY=DG_KEY, CARTESIA_API_KEY=CA_KEY, CARTESIA_VOICE_ID=VOICE,
                 **overrides)


def test_defaults_follow_adr_0009() -> None:
    s = _load()
    assert (s.stt_provider, s.tts_provider) == ("deepgram", "cartesia")
    assert (s.deepgram_model, s.deepgram_language, s.cartesia_model) == ("nova-3", "hi",
                                                                         "sonic-3.6")
    assert s.deepgram_api_key == "" and s.cartesia_api_key == ""


def test_keys_and_overrides_are_read() -> None:
    s = _full(DEEPGRAM_LANGUAGE="Multi", CARTESIA_MODEL="sonic-3.6-2026-08-27")
    assert (s.deepgram_api_key, s.cartesia_api_key, s.cartesia_voice_id) == (DG_KEY, CA_KEY,
                                                                             VOICE)
    assert s.deepgram_language == "multi"
    assert s.cartesia_model == "sonic-3.6-2026-08-27"


def test_keys_hidden_from_repr() -> None:
    text = repr(_full())
    assert DG_KEY not in text and CA_KEY not in text


def test_invalid_provider_rejected() -> None:
    with pytest.raises(ConfigError, match="STT_PROVIDER"):
        _load(STT_PROVIDER="whisper")
    with pytest.raises(ConfigError, match="TTS_PROVIDER"):
        _load(TTS_PROVIDER="bulbul")


@pytest.mark.parametrize(
    ("missing", "message"),
    [("DEEPGRAM_API_KEY", "DEEPGRAM_API_KEY"), ("CARTESIA_API_KEY", "CARTESIA_API_KEY"),
     ("CARTESIA_VOICE_ID", "CARTESIA_VOICE_ID")],
)
def test_check_speech_keys_names_the_missing_value(missing: str, message: str) -> None:
    values = {"DEEPGRAM_API_KEY": DG_KEY, "CARTESIA_API_KEY": CA_KEY, "CARTESIA_VOICE_ID": VOICE}
    values[missing] = ""
    with pytest.raises(ConfigError, match=message):
        check_speech_keys(_load(**values))


def test_check_speech_keys_passes_and_sarvam_needs_nothing_new() -> None:
    check_speech_keys(_full())
    check_speech_keys(_load(STT_PROVIDER="sarvam", TTS_PROVIDER="sarvam"))


def test_cartesia_sample_rate_checked() -> None:
    with pytest.raises(ConfigError, match="TTS_SAMPLE_RATE"):
        check_speech_keys(_full(TTS_SAMPLE_RATE="12000"))


def test_yaml_refuses_any_api_key(tmp_path: Path) -> None:
    yaml_file = tmp_path / "settings.yaml"
    yaml_file.write_text("stt:\n  deepgram_api_key: leak\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="DEEPGRAM_API_KEY"):
        load_settings(env_file=None, overrides={"SARVAM_API_KEY": "sk_abc"},
                      settings_file=yaml_file)


def test_logs_mask_deepgram_and_cartesia_headers() -> None:
    text = mask_secrets(f"Authorization: Token {DG_KEY} / Bearer {CA_KEY}")
    assert DG_KEY not in text and CA_KEY not in text
    assert "Token ***" in text


def test_audit_log_redacts_speech_keys(tmp_path: Path) -> None:
    settings = replace(_full(), log_dir=tmp_path)
    audit = AuditLog.from_settings(settings)
    assert DG_KEY in audit._secrets and CA_KEY in audit._secrets


def test_build_clients_for_each_provider() -> None:
    client = SarvamClient("sk_abc")
    try:
        new = _full()
        tts = main.build_tts(new, client)
        batch = main.build_batch_stt(new, client)
        assert isinstance(tts, CartesiaTTS) and tts.voice_id == VOICE
        assert tts.default_language == "hi" and tts.sample_rate == 24000
        assert isinstance(batch, DeepgramBatchSTT)
        tts.close()
        batch.close()

        old = _load(STT_PROVIDER="sarvam", TTS_PROVIDER="sarvam")
        assert isinstance(main.build_tts(old, client), SarvamTTS)
        assert isinstance(main.build_batch_stt(old, client), RestSTT)
    finally:
        client.close()


def test_cartesia_speed_is_clamped_from_tts_pace() -> None:
    client = SarvamClient("sk_abc")
    try:
        tts = main.build_tts(_full(TTS_PACE="2.0"), client)
        assert tts.speed == 1.5
        tts.close()
    finally:
        client.close()

"""Tests for Deepgram keyterm prompting: cleaning, request parameters, settings, wiring."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

import main
from core.config import Settings, load_settings
from stt.deepgram_batch import DeepgramBatchSTT
from stt.deepgram_realtime import DeepgramRealtimeSTT
from stt.keyterms import MAX_KEYTERMS, clean_keyterms, keyterm_params


@pytest.fixture(autouse=True)
def _no_env_keyterms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPGRAM_KEYTERMS", raising=False)


def _load(**overrides: str) -> Settings:
    return load_settings(env_file=None, overrides={"SARVAM_API_KEY": "sk_abc", **overrides},
                         settings_file=None)


def test_clean_keyterms_trims_dedupes_and_drops_bad_terms() -> None:
    terms = ["  Chrome ", "chrome", "", "Arijit   Singh", "a,b", "term:0.5", "x;y",
             "N" * 41, "Notepad"]
    assert clean_keyterms(terms) == ["Chrome", "Arijit Singh", "Notepad"]


def test_clean_keyterms_caps_the_count() -> None:
    assert len(clean_keyterms(f"term{i}" for i in range(80))) == MAX_KEYTERMS


def test_keyterm_params_one_per_term() -> None:
    assert keyterm_params(["Chrome", "Arijit Singh"]) == [("keyterm", "Chrome"),
                                                         ("keyterm", "Arijit Singh")]


def test_streaming_url_repeats_keyterm(settings: Settings) -> None:
    stt = DeepgramRealtimeSTT("dg_key", settings, lambda t: None, lambda t, lang: None,
                              keyterms=["Chrome", "Arijit Singh", "chrome"])
    query = parse_qs(urlsplit(stt.url()).query)
    assert query["keyterm"] == ["Chrome", "Arijit Singh"]
    assert query["language"] == ["hi"] and query["model"] == ["nova-3"]


def test_streaming_url_without_keyterms(settings: Settings) -> None:
    stt = DeepgramRealtimeSTT("dg_key", settings, lambda t: None, lambda t, lang: None)
    assert "keyterm" not in stt.url()


def test_batch_request_repeats_keyterm() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = {"results": {"channels": [{"alternatives": [{"transcript": "ok"}]}]}}
        return httpx.Response(200, content=json.dumps(body))

    stt = DeepgramBatchSTT("dg_key", keyterms=["Chrome", "YouTube"],
                           transport=httpx.MockTransport(handler))
    stt.transcribe(b"\x00\x00" * 16000, 16000)
    assert seen[0].url.params.get_list("keyterm") == ["Chrome", "YouTube"]
    assert seen[0].url.params["language"] == "multi"


def test_settings_extra_terms_and_off_switch() -> None:
    s = _load(DEEPGRAM_KEYTERMS="Arijit Singh, Lata Mangeshkar ,")
    assert s.deepgram_keyterms_enabled
    assert s.deepgram_keyterms == ("Arijit Singh", "Lata Mangeshkar")
    off = _load(DEEPGRAM_KEYTERMS="None")
    assert not off.deepgram_keyterms_enabled and off.deepgram_keyterms == ()
    assert _load().deepgram_keyterms_enabled


def test_speech_keyterms_from_allowlist_services_and_extras() -> None:
    terms = main.speech_keyterms(_load(DEEPGRAM_KEYTERMS="Arijit Singh"))
    for term in ("Notepad", "Chrome", "PowerPoint", "File Explorer", "YouTube", "Google",
                 "Arijit Singh"):
        assert term in terms
    assert "Powerpoint" not in terms and "File Manager" not in terms
    assert main.speech_keyterms(_load(DEEPGRAM_KEYTERMS="none")) == []


def test_speech_keyterms_survive_a_missing_allowlist(tmp_path: Any) -> None:
    terms = main.speech_keyterms(_load(ALLOWLIST_PATH=str(tmp_path / "missing.yaml")))
    assert terms == ["YouTube", "Google"]


def test_built_clients_get_the_keyterms() -> None:
    from sarvam.client import SarvamClient

    settings = _load(DEEPGRAM_API_KEY="dg_key", DEEPGRAM_KEYTERMS="Arijit Singh")
    client = SarvamClient("sk_abc")
    try:
        batch = main.build_batch_stt(settings, client)
        assert "Arijit Singh" in batch.keyterms and "Chrome" in batch.keyterms
        batch.close()
    finally:
        client.close()

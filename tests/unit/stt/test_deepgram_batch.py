"""Tests for the Deepgram push-to-talk client with a fake HTTP transport (no network)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from core.interfaces import BatchSpeechToText
from language import from_provider_code
from stt.deepgram_batch import DeepgramBatchSTT, DeepgramError, parse_response

KEY = "dg_test_key_123"
ONE_SECOND = b"\x00\x00" * 16000


def body(transcript: str, languages: list[str] | None = None) -> dict[str, Any]:
    alternative: dict[str, Any] = {"transcript": transcript, "confidence": 0.9}
    if languages is not None:
        alternative["languages"] = languages
    return {"results": {"channels": [{"alternatives": [alternative]}]}}


def make_stt(handler: Any, hint: str = "") -> DeepgramBatchSTT:
    return DeepgramBatchSTT(
        KEY, language_hint=hint, transport=httpx.MockTransport(handler), sleep=lambda s: None
    )


def test_request_shape_and_result() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body("Chrome kholo", ["hi", "en"]))

    assert make_stt(handler).transcribe(ONE_SECOND, 16000) == ("Chrome kholo", "hi-IN")
    request = seen[0]
    assert request.url.host == "api.deepgram.com" and request.url.path == "/v1/listen"
    assert request.url.params["model"] == "nova-3"
    assert request.url.params["language"] == "multi"
    assert request.headers["Authorization"] == f"Token {KEY}"
    assert request.headers["Content-Type"] == "audio/wav"
    assert request.content[:4] == b"RIFF"


def test_english_maps_to_en_in() -> None:
    stt = make_stt(lambda r: httpx.Response(200, json=body("Open Chrome.", ["en"])))
    assert stt.transcribe(ONE_SECOND, 16000) == ("Open Chrome.", "en-IN")


def test_no_language_falls_back_to_hint() -> None:
    stt = make_stt(lambda r: httpx.Response(200, json=body("", [])), hint="hi-IN")
    assert stt.transcribe(ONE_SECOND, 16000) == ("", "hi-IN")


def test_too_short_skips_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    assert make_stt(handler).transcribe(b"\x00" * 100, 16000) == ("", None)


def test_retries_once_on_server_error() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, json={"err_msg": "busy"})
        return httpx.Response(200, json=body("namaste", ["hi"]))

    assert make_stt(handler).transcribe(ONE_SECOND, 16000) == ("namaste", "hi-IN")
    assert len(calls) == 2


def test_bad_key_fails_fast_without_leaking_key() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, json={"err_msg": "Invalid credentials."})

    stt = make_stt(handler)
    with pytest.raises(DeepgramError) as info:
        stt.transcribe(ONE_SECOND, 16000)
    assert info.value.status == 401 and not info.value.retryable
    assert len(calls) == 1
    assert KEY not in str(info.value)
    assert not stt.health().ok and KEY not in stt.health().detail


def test_network_error_retried_then_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    stt = make_stt(handler)
    with pytest.raises(DeepgramError) as info:
        stt.transcribe(ONE_SECOND, 16000)
    assert info.value.status == 0 and info.value.retryable
    assert not stt.health().ok


def test_health_recovers_after_success() -> None:
    responses = iter([httpx.Response(400, json={"err_msg": "bad"}),
                      httpx.Response(200, json=body("ok", ["en"]))])
    stt = make_stt(lambda r: next(responses))
    with pytest.raises(DeepgramError):
        stt.transcribe(ONE_SECOND, 16000)
    stt.transcribe(ONE_SECOND, 16000)
    assert stt.health().ok


@pytest.mark.parametrize("raw", [{}, {"results": {}}, {"results": {"channels": []}}, None, []])
def test_parse_response_tolerates_bad_bodies(raw: Any) -> None:
    assert parse_response(raw) == ("", None)


def test_empty_key_rejected() -> None:
    with pytest.raises(ValueError):
        DeepgramBatchSTT("  ")


def test_matches_protocol() -> None:
    stt: BatchSpeechToText = make_stt(lambda r: httpx.Response(200, json=body("")))
    assert callable(stt.transcribe)


@pytest.mark.parametrize(
    ("code", "expected"),
    [("hi", "hi-IN"), ("en", "en-IN"), ("en-US", "en-IN"), ("hi-Latn", "hi-IN"),
     ("or", "od-IN"), ("", None), (None, None), ("unknown", None)],
)
def test_from_provider_code(code: str | None, expected: str | None) -> None:
    assert from_provider_code(code) == expected

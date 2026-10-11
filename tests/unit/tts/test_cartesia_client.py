"""Tests for the Cartesia TTS client with a fake HTTP transport (no network)."""

from __future__ import annotations

import json
import threading
from typing import Any

import httpx
import pytest

from core.interfaces import TextToSpeech
from language.detect import to_cartesia_language
from tts.cartesia_client import CartesiaError, CartesiaTTS

KEY = "sk_car_test_key_123"
VOICE = "voice-hi-1"
PCM = b"\x01\x02" * 2400  # 0.1 s at 24 kHz


def make_tts(handler: Any, **kwargs: Any) -> CartesiaTTS:
    return CartesiaTTS(
        KEY, VOICE, transport=httpx.MockTransport(handler), sleep=lambda s: None,
        clean=False, **kwargs,
    )


def test_request_shape() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=PCM)

    audio = b"".join(make_tts(handler).stream("Chrome khol raha hoon.", "hi-IN"))
    assert audio == PCM
    request = seen[0]
    assert str(request.url) == "https://api.cartesia.ai/tts/bytes"
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert request.headers["Cartesia-Version"] == "2026-08-14"
    body = json.loads(request.content)
    assert body == {
        "model_id": "sonic-3.6",
        "transcript": "Chrome khol raha hoon.",
        "voice": {"id": VOICE},
        "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 24000},
        "language": "hi",
    }


def test_speed_and_per_language_voice() -> None:
    tts = make_tts(lambda r: httpx.Response(200, content=PCM),
                   speed=1.2, voices={"en": "voice-en-1"})
    body = tts.payload("Opening Chrome.", "en-IN")
    assert body["language"] == "en" and body["voice"] == {"id": "voice-en-1"}
    assert body["generation_config"] == {"speed": 1.2}
    assert tts.payload("नमस्ते", None)["language"] == "hi"


def test_one_request_per_sentence_in_order() -> None:
    texts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        texts.append(json.loads(request.content)["transcript"])
        return httpx.Response(200, content=PCM)

    reply = "Chrome khol raha hoon, ek second rukiye. Volume abhi chalees percent hai."
    audio = b"".join(make_tts(handler).stream(reply, "hi-IN"))
    assert len(texts) == 2 and texts[0].startswith("Chrome") and texts[1].startswith("Volume")
    assert audio == PCM * 2


def test_odd_byte_chunks_stay_sample_aligned() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(b"\x01\x02\x03"))

    chunks = list(make_tts(handler).stream("Hi.", "en-IN"))
    assert all(len(c) % 2 == 0 for c in chunks)


def test_retries_once_before_audio() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, json={"error": "busy"})
        return httpx.Response(200, content=PCM)

    assert b"".join(make_tts(handler).stream("Hello.", "en-IN")) == PCM
    assert len(calls) == 2


def test_bad_key_raises_without_leaking_key() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, json={"error": "Invalid API key"})

    tts = make_tts(handler)
    with pytest.raises(CartesiaError) as info:
        list(tts.stream("Hello.", "en-IN"))
    assert info.value.status == 401 and not info.value.retryable
    assert len(calls) == 1
    assert KEY not in str(info.value)
    assert not tts.health().ok and KEY not in tts.health().detail


def test_network_error_retried_then_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    tts = make_tts(handler)
    with pytest.raises(CartesiaError) as info:
        list(tts.stream("Hello.", "en-IN"))
    assert info.value.status == 0 and info.value.retryable


def test_health_recovers() -> None:
    responses = iter([httpx.Response(400, json={"error": "bad"}),
                      httpx.Response(200, content=PCM)])
    tts = make_tts(lambda r: next(responses))
    with pytest.raises(CartesiaError):
        list(tts.stream("Hello.", "en-IN"))
    list(tts.stream("Hello.", "en-IN"))
    assert tts.health().ok


def test_cancel_stops_remaining_sentences() -> None:
    requests: list[int] = []
    release = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(1)
        release.wait(1.0)
        return httpx.Response(200, content=PCM)

    tts = make_tts(handler, prefetch_chunks=1)
    stream = tts.stream("First sentence here. Second sentence here. Third one here.", "en-IN")
    release.set()
    assert next(stream) == PCM
    tts.cancel()
    assert list(stream) == []
    assert stream.join(2.0)
    assert len(requests) < 3


def test_cleaner_runs_by_default() -> None:
    texts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        texts.append(json.loads(request.content)["transcript"])
        return httpx.Response(200, content=PCM)

    tts = CartesiaTTS(KEY, VOICE, transport=httpx.MockTransport(handler))
    list(tts.stream("**Volume** 40% hai.", "hi-IN"))
    assert "**" not in texts[0] and "%" not in texts[0]


@pytest.mark.parametrize(
    "kwargs", [{"api_key": " "}, {"voice_id": ""}, {"sample_rate": 12345}, {"speed": 2.0}]
)
def test_invalid_config_rejected(kwargs: dict[str, Any]) -> None:
    args = {"api_key": KEY, "voice_id": VOICE, **kwargs}
    with pytest.raises(ValueError):
        CartesiaTTS(args.pop("api_key"), args.pop("voice_id"), **args)


def test_matches_protocol() -> None:
    tts: TextToSpeech = make_tts(lambda r: httpx.Response(200, content=PCM))
    assert tts.sample_rate == 24000 and callable(tts.stream)


@pytest.mark.parametrize(
    ("code", "expected"),
    [("hi-IN", "hi"), ("en-IN", "en"), ("ta-IN", "ta"), ("od-IN", "or"), ("as-IN", "bn"),
     (None, "hi"), ("", "hi"), ("xx-IN", "hi")],
)
def test_to_cartesia_language(code: str | None, expected: str) -> None:
    assert to_cartesia_language(code) == expected

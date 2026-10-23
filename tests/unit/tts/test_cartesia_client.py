"""Tests for the Cartesia TTS client with a fake HTTP transport (no network)."""

from __future__ import annotations

import json
import threading
import time
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
        KEY, VOICE, transport=httpx.MockTransport(handler), clean=False, **kwargs,
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


# -- cache -------------------------------------------------------------------------


def counting(content: bytes = PCM) -> tuple[list[str], Any]:
    texts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        texts.append(json.loads(request.content)["transcript"])
        return httpx.Response(200, content=content)

    return texts, handler


def test_repeated_sentence_comes_from_cache() -> None:
    texts, handler = counting()
    tts = make_tts(handler)
    first = b"".join(tts.stream("Notepad khul gaya.", "hi-IN"))
    second = b"".join(tts.stream("Notepad khul gaya.", "hi-IN"))
    assert first == second == PCM
    assert texts == ["Notepad khul gaya."]


def test_cache_key_includes_language() -> None:
    texts, handler = counting()
    tts = make_tts(handler)
    list(tts.stream("Okay.", "hi-IN"))
    list(tts.stream("Okay.", "en-IN"))
    assert len(texts) == 2


def test_long_sentences_and_disabled_cache_are_not_cached() -> None:
    long_text = "Yeh ek bahut lamba vakya hai jo cache mein nahi rakha jayega kyunki lamba hai."
    assert len(long_text) > 60
    texts, handler = counting()
    tts = make_tts(handler, cache_max_chars=60)
    list(tts.stream(long_text, "hi-IN"))
    list(tts.stream(long_text, "hi-IN"))
    assert len(texts) == 2
    texts, handler = counting()
    tts = make_tts(handler, cache_entries=0)
    list(tts.stream("Okay.", "en-IN"))
    list(tts.stream("Okay.", "en-IN"))
    assert len(texts) == 2


def test_cache_evicts_least_recently_used() -> None:
    texts, handler = counting()
    tts = make_tts(handler, cache_entries=2)
    for text in ("One.", "Two.", "One.", "Three.", "Two."):
        list(tts.stream(text, "en-IN"))
    # "One." stays (used again), "Two." was evicted by "Three." and is fetched again.
    assert texts == ["One.", "Two.", "Three.", "Two."]


class _BreaksMidway(httpx.SyncByteStream):
    def __iter__(self) -> Any:
        yield PCM
        raise httpx.ReadError("connection reset")


def test_broken_sentence_is_not_cached() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(200, stream=_BreaksMidway())
        return httpx.Response(200, content=PCM)

    tts = make_tts(handler)
    assert b"".join(tts.stream("Hello.", "en-IN")) == PCM  # half spoken, not repeated
    list(tts.stream("Hello.", "en-IN"))
    assert len(calls) == 2


def test_cached_audio_is_chunked_for_interrupts() -> None:
    long_pcm = b"\x01\x02" * 24000  # 1 s
    _, handler = counting(long_pcm)
    tts = make_tts(handler)
    list(tts.stream("Hello.", "en-IN"))
    chunks = list(tts.stream("Hello.", "en-IN"))
    assert len(chunks) == 10 and b"".join(chunks) == long_pcm


# -- hedged requests ---------------------------------------------------------------


def slow_then_fast(delays: list[float]) -> tuple[list[float], Any]:
    """Request N waits delays[N] seconds, then answers with N+1 repeated as audio."""
    started: list[float] = []
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            index = len(started)
            started.append(time.perf_counter())
        time.sleep(delays[index])
        return httpx.Response(200, content=bytes([index + 1, 0]) * 100)

    return started, handler


def test_slow_answer_gets_a_second_request_and_the_fast_one_wins() -> None:
    started, handler = slow_then_fast([1.5, 0.0])
    tts = make_tts(handler, hedge_after_s=0.1)
    begin = time.perf_counter()
    audio = b"".join(tts.stream("Notepad khul gaya.", "hi-IN"))
    elapsed = time.perf_counter() - begin
    assert len(started) == 2
    assert audio == bytes([2, 0]) * 100  # the second request's audio
    assert elapsed < 1.0
    tts.close()


def test_no_second_request_when_the_first_is_fast() -> None:
    started, handler = slow_then_fast([0.0, 0.0])
    tts = make_tts(handler, hedge_after_s=0.5)
    assert b"".join(tts.stream("Hello.", "en-IN")) == bytes([1, 0]) * 100
    assert len(started) == 1


def test_hedging_can_be_turned_off() -> None:
    started, handler = slow_then_fast([0.4, 0.0])
    tts = make_tts(handler, hedge_after_s=0)
    assert b"".join(tts.stream("Hello.", "en-IN")) == bytes([1, 0]) * 100
    assert len(started) == 1


def test_cancel_while_waiting_returns_promptly() -> None:
    started, handler = slow_then_fast([2.0, 2.0])
    tts = make_tts(handler, hedge_after_s=0)
    stream = tts.stream("Hello.", "en-IN")
    threading.Timer(0.2, stream.cancel).start()
    begin = time.perf_counter()
    assert list(stream) == []
    assert time.perf_counter() - begin < 1.0
    tts.close()

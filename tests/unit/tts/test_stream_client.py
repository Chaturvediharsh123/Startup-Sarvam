"""Tests for the Bulbul TTS client with fake HTTP: payload, fallback, voices, prefetch, cancel."""

from __future__ import annotations

import base64
import io
import json
import threading
import time
import wave
from typing import Any

import httpx
import pytest

from core.config import Settings, load_settings
from sarvam.client import SarvamClient, SarvamError
from tts.stream_client import (
    SarvamTTS,
    SpeechStream,
    _pcm_chunks,
    split_sentences,
    wav_data_offset,
    wav_to_pcm,
)


def wav_bytes(pcm: bytes, rate: int = 24000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buffer.getvalue()


def client_for(handler: Any) -> SarvamClient:
    return SarvamClient("sk_test", transport=httpx.MockTransport(handler), sleep=lambda s: None)


TWO_SENTENCES = "Pehla vaakya thoda lamba hai yahan par. Doosra vaakya bhi kaafi lamba hai yahan."


# -- helpers kept from the original client ------------------------------------------


def test_wav_header_detection() -> None:
    wav = wav_bytes(b"\x01\x00" * 10)
    assert wav_data_offset(wav) == 44
    assert wav_data_offset(b"\x01\x02\x03\x04" * 4) == 0
    assert wav_data_offset(b"RIF") is None
    assert wav_data_offset(wav[:20]) is None


def test_pcm_chunks_strip_header_and_align() -> None:
    pcm = bytes(range(200))
    wav = wav_bytes(pcm)
    pieces = [wav[:10], wav[10:47], wav[47:101], wav[101:]]
    out = list(_pcm_chunks(iter(pieces)))
    assert b"".join(out) == pcm
    assert all(len(c) % 2 == 0 for c in out)


def test_pcm_chunks_raw_pcm_passthrough() -> None:
    assert b"".join(_pcm_chunks(iter([b"\x01\x02\x03", b"\x04"]))) == b"\x01\x02\x03\x04"


def test_wav_to_pcm() -> None:
    pcm, rate = wav_to_pcm(wav_bytes(b"\x01\x00" * 4, 16000))
    assert pcm == b"\x01\x00" * 4 and rate == 16000


def test_split_sentences_still_importable_here() -> None:
    from tts import sentence_splitter

    assert split_sentences is sentence_splitter.split_sentences


# -- requests ---------------------------------------------------------------------------


def test_tts_stream_payload(settings: Settings) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        assert request.url.path == "/text-to-speech/stream"
        return httpx.Response(200, content=b"\x00\x01" * 50)

    tts = SarvamTTS(client_for(handler), settings)
    audio = b"".join(tts.stream("नमस्ते, नोटपैड खुल गया है।", "hi-IN"))
    assert audio == b"\x00\x01" * 50
    body = seen[0]
    assert body["model"] == "bulbul:v3"
    assert body["output_audio_codec"] == "linear16"
    assert body["speech_sample_rate"] == 24000
    assert body["language_code"] == "hi-IN"
    assert body["speaker"] == settings.tts_speaker
    assert tts.health().ok


def test_tts_maps_unsupported_language(settings: Settings) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["language_code"])
        return httpx.Response(200, content=b"\x00\x00")

    list(SarvamTTS(client_for(handler), settings).stream("hello", "or-IN"))
    list(SarvamTTS(client_for(handler), settings).stream("hello", "sat-IN"))
    assert seen == ["od-IN", "en-IN"]


def test_voice_per_language() -> None:
    settings = load_settings(
        env_file=None, settings_file=None,
        overrides={"SARVAM_API_KEY": "sk_test", "TTS_SPEAKER": "priya",
                   "TTS_VOICES": "ta-IN:kavya,bn:ishita"},
    )
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((body["language_code"], body["speaker"]))
        return httpx.Response(200, content=b"\x00\x00")

    tts = SarvamTTS(client_for(handler), settings)
    for language in ("ta-IN", "bn-IN", "en-IN", None):
        list(tts.stream("vanakkam", language))
    assert seen == [("ta-IN", "kavya"), ("bn-IN", "ishita"), ("en-IN", "priya"),
                    ("en-IN", "priya")]


def test_text_is_cleaned_before_sending(settings: Settings) -> None:
    texts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        texts.append(json.loads(request.content)["text"])
        return httpx.Response(200, content=b"\x00\x00")

    list(SarvamTTS(client_for(handler), settings).stream("**Volume** 40% kar diya 👍", "hi-IN"))
    assert texts == ["Volume chaalees percent kar diya"]
    texts.clear()
    raw = SarvamTTS(client_for(handler), settings, clean=False)
    list(raw.stream("Volume 40%", "hi-IN"))
    assert texts == ["Volume 40%"]


def test_tts_rest_fallback(settings: Settings) -> None:
    pcm = b"\x05\x00" * 3000

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/text-to-speech/stream":
            return httpx.Response(400, json={"error": {"message": "bad"}})
        assert json.loads(request.content)["output_audio_codec"] == "wav"
        return httpx.Response(200, json={"audios": [base64.b64encode(wav_bytes(pcm)).decode()]})

    audio = b"".join(SarvamTTS(client_for(handler), settings).stream("hello there", "en-IN"))
    assert audio == pcm


def test_both_endpoints_failing_raises_and_marks_health(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    tts = SarvamTTS(client_for(handler), settings)
    with pytest.raises(SarvamError):
        list(tts.stream("hello there", "en-IN"))
    assert not tts.health().ok


def test_empty_text_yields_nothing(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    assert list(SarvamTTS(client_for(handler), settings).stream(" 😀 ", "hi-IN")) == []


# -- prefetch and cancel -------------------------------------------------------------------


def test_next_sentence_is_fetched_while_first_plays(settings: Settings) -> None:
    requested: list[str] = []
    second_requested = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(json.loads(request.content)["text"])
        if len(requested) == 2:
            second_requested.set()
        return httpx.Response(200, content=b"\x00\x00" * 100)

    stream = SarvamTTS(client_for(handler), settings).stream(TWO_SENTENCES, "hi-IN")
    first = next(stream)  # sentence 1 is "playing"; nothing else consumed yet
    assert first
    assert second_requested.wait(2.0)  # sentence 2 was requested in the background
    assert len(list(stream)) == 1
    assert len(requested) == 2


def test_prefetch_queue_is_bounded(settings: Settings) -> None:
    requests: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(1)
        return httpx.Response(200, content=(b"\x00\x00" * 50 for _ in range(20)))

    tts = SarvamTTS(client_for(handler), settings, prefetch_chunks=2)
    stream = tts.stream(TWO_SENTENCES, "hi-IN")
    next(stream)
    time.sleep(0.2)
    assert requests == [1]  # producer is held back by the full queue
    stream.close()
    assert stream.join(1.0)


def test_close_stops_prefetch_promptly(settings: Settings) -> None:
    requests: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(1)
        return httpx.Response(200, content=(b"\x00\x00" * 50 for _ in range(200)))

    stream = SarvamTTS(client_for(handler), settings, prefetch_chunks=4).stream(
        TWO_SENTENCES, "hi-IN"
    )
    next(stream)
    started = time.monotonic()
    stream.close()
    assert stream.join(1.0)
    assert time.monotonic() - started < 0.5
    assert requests == [1]  # sentence 2 never requested
    assert list(stream) == []


def test_cancel_unblocks_a_waiting_consumer(settings: Settings) -> None:
    release = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        release.wait(5.0)  # the network is slow
        return httpx.Response(200, content=b"\x00\x00")

    tts = SarvamTTS(client_for(handler), settings)
    stream = tts.stream("hello there, how are you", "en-IN")
    result: list[list[bytes]] = []
    thread = threading.Thread(target=lambda: result.append(list(stream)))
    thread.start()
    time.sleep(0.1)
    started = time.monotonic()
    tts.cancel()
    thread.join(1.0)
    assert time.monotonic() - started < 0.1
    assert result == [[]]
    release.set()
    assert stream.join(2.0)


def test_speech_stream_reraises_producer_error() -> None:
    def produce(job: SpeechStream) -> None:
        job.put(b"\x00\x00")
        raise SarvamError("boom")

    stream = SpeechStream(produce)
    assert next(stream) == b"\x00\x00"
    with pytest.raises(SarvamError):
        next(stream)
    with pytest.raises(StopIteration):
        next(stream)


def test_cancel_hook_runs_once() -> None:
    calls: list[int] = []
    stream = SpeechStream(lambda job: None)
    stream.add_cancel_hook(lambda: calls.append(1))
    stream.cancel()
    stream.cancel()
    stream.add_cancel_hook(lambda: calls.append(2))  # already cancelled: runs at once
    assert calls == [1, 2]

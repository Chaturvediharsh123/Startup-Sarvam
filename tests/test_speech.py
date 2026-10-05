"""Tests for Sarvam TTS and STT clients with fake HTTP and fake WebSockets."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import struct
import threading
import wave
from typing import Any

import httpx
import pytest

from assistant.config import Settings
from sarvam.client import SarvamClient
from sarvam.stt_realtime import FatalSTTError, RealtimeSTT
from sarvam.stt_rest import RestSTT, pcm_to_wav
from sarvam.tts import SarvamTTS, _pcm_chunks, split_sentences, wav_data_offset, wav_to_pcm


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


# -- TTS ---------------------------------------------------------------------------


def test_split_sentences() -> None:
    text = "नमस्ते। मैं आपकी मदद करूँगा। Notepad khul gaya! Kya aur kuch chahiye?"
    parts = split_sentences(text)
    assert parts[0].startswith("नमस्ते। मैं")  # short first piece merged with the next
    assert "".join(parts).replace(" ", "") == text.replace(" ", "")
    long = " ".join(["word"] * 300)
    assert all(len(p) <= 450 for p in split_sentences(long))
    assert split_sentences("   ") == []


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


def test_tts_maps_unsupported_language(settings: Settings) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["language_code"])
        return httpx.Response(200, content=b"\x00\x00")

    list(SarvamTTS(client_for(handler), settings).stream("hello", "or-IN"))
    list(SarvamTTS(client_for(handler), settings).stream("hello", "sat-IN"))
    assert seen == ["od-IN", "en-IN"]


def test_tts_rest_fallback(settings: Settings) -> None:
    pcm = b"\x05\x00" * 3000

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/text-to-speech/stream":
            return httpx.Response(400, json={"error": {"message": "bad"}})
        assert json.loads(request.content)["output_audio_codec"] == "wav"
        return httpx.Response(200, json={"audios": [base64.b64encode(wav_bytes(pcm)).decode()]})

    audio = b"".join(SarvamTTS(client_for(handler), settings).stream("hello there", "en-IN"))
    assert audio == pcm


def test_wav_to_pcm() -> None:
    pcm, rate = wav_to_pcm(wav_bytes(b"\x01\x00" * 4, 16000))
    assert pcm == b"\x01\x00" * 4 and rate == 16000


# -- REST STT ----------------------------------------------------------------------


def test_rest_stt_request(settings: Settings) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"transcript": " नोटपैड खोलो ", "language_code": "hi-IN"})

    stt = RestSTT(client_for(handler), settings)
    text, lang = stt.transcribe(b"\x00\x00" * 16000, 16000)
    assert (text, lang) == ("नोटपैड खोलो", "hi-IN")
    body = seen[0].content
    assert seen[0].url.path == "/speech-to-text"
    assert b'name="model"' in body and b"saaras:v4" in body
    assert b'name="language_code"' in body and b"unknown" in body
    assert b"RIFF" in body


def test_rest_stt_too_short_skips_call(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not be called")

    assert RestSTT(client_for(handler), settings).transcribe(b"\x00" * 100, 16000) == ("", None)


def test_rest_stt_trims_long_audio(settings: Settings) -> None:
    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sizes.append(len(request.content))
        return httpx.Response(200, json={"transcript": "x"})

    RestSTT(client_for(handler), settings).transcribe(b"\x00\x00" * 16000 * 40, 16000)
    assert sizes[0] < 16000 * 2 * 30 + 2000


def test_pcm_to_wav_header() -> None:
    wav = pcm_to_wav(b"\x00\x00" * 10, 16000)
    assert wav[:4] == b"RIFF"
    assert struct.unpack("<I", wav[24:28])[0] == 16000


# -- realtime STT ------------------------------------------------------------------


class FakeWebSocket:
    """Async context manager + iterator that replays server messages."""

    def __init__(self, messages: list[dict[str, Any]], stop: threading.Event) -> None:
        self.messages = messages
        self.sent: list[dict[str, Any]] = []
        self.stop = stop
        self.closed = False

    async def __aenter__(self) -> FakeWebSocket:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True

    def __aiter__(self) -> FakeWebSocket:
        return self

    async def __anext__(self) -> str:
        if self.messages:
            await asyncio.sleep(0.01)
            return json.dumps(self.messages.pop(0))
        self.stop.set()  # script finished: ask the client to shut down
        await asyncio.sleep(0.5)
        raise StopAsyncIteration

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def close(self) -> None:
        self.closed = True


def _stt(settings: Settings, ws: FakeWebSocket, record: dict[str, list[Any]]) -> RealtimeSTT:
    def connect(url: str, additional_headers: dict[str, str]) -> FakeWebSocket:
        record["urls"].append(url)
        record["headers"].append(additional_headers)
        return ws

    return RealtimeSTT(
        SarvamClient("sk_test"),
        settings,
        on_partial=record["partial"].append,
        on_final=lambda t, lang: record["final"].append((t, lang)),
        on_event=record["events"].append,
        connect=connect,
    )


def _record() -> dict[str, list[Any]]:
    return {"urls": [], "headers": [], "partial": [], "final": [], "events": []}


def test_realtime_session_flow(settings: Settings) -> None:
    stop = threading.Event()
    ws = FakeWebSocket(
        [
            {"event": "session.begin", "request_id": "r1"},
            {"event": "vad.speech_start", "utterance_idx": 0},
            {"event": "transcript.partial", "utterance_idx": 0, "text": "नोटपैड",
             "language": "hi-IN"},
            {"event": "vad.speech_end", "utterance_idx": 0},
            {"event": "transcript.final", "utterance_idx": 0, "text": "नोटपैड खोलो",
             "language": "hi-IN", "language_confidence": 0.98},
            {"event": "transcript.final", "utterance_idx": 1, "text": "  "},
        ],
        stop,
    )
    chunks = [b"\x01\x00" * 1600, b"\x02\x00" * 1600]
    record = _record()
    stt = _stt(settings, ws, record)
    stt.run_blocking(lambda timeout: chunks.pop(0) if chunks else None, stop)

    url = record["urls"][0]
    assert url.startswith("wss://api.sarvam.ai/speech-to-text-realtime/ws?")
    for part in ("language_code=auto", "model=saaras%3Av3-realtime", "sample_rate=16000",
                 "encoding=linear16", "endpointing=vad", "stream_type=fast"):
        assert part in url
    assert record["headers"][0] == {"api-subscription-key": "sk_test"}
    assert record["partial"] == ["नोटपैड"]
    assert record["final"] == [("नोटपैड खोलो", "hi-IN")]
    assert {"connected", "speech_start", "speech_end"} <= set(record["events"])
    audio = [m for m in ws.sent if m["event"] == "audio_input"]
    assert base64.b64decode(audio[0]["audio"]) == b"\x01\x00" * 1600
    assert ws.sent[-1] == {"event": "end"}
    assert ws.closed


def test_fatal_error_event_stops(settings: Settings) -> None:
    stt = _stt(settings, FakeWebSocket([], threading.Event()), _record())
    with pytest.raises(FatalSTTError):
        stt.handle_message({"event": "error", "code": "4000", "is_fatal": True, "message": "bad"})
    stt.handle_message({"event": "error", "code": "x", "is_fatal": False, "message": "meh"})


def test_reconnects_after_drop_then_stops(settings: Settings) -> None:
    stop = threading.Event()
    attempts: list[int] = []

    class Dropping:
        async def __aenter__(self) -> Dropping:
            attempts.append(1)
            if len(attempts) >= 2:
                stop.set()
            raise OSError("network down")

        async def __aexit__(self, *exc: object) -> None:
            return None

    events: list[str] = []
    stt = RealtimeSTT(
        SarvamClient("sk_test"), settings, lambda t: None, lambda t, lang: None,
        events.append, connect=lambda url, additional_headers: Dropping(),
    )
    stt.run_blocking(lambda timeout: None, stop)
    assert len(attempts) == 2
    assert events.count("disconnected") == 2


def test_fatal_session_does_not_reconnect(settings: Settings) -> None:
    attempts: list[int] = []

    class Rejecting(FakeWebSocket):
        async def __aenter__(self) -> Rejecting:
            attempts.append(1)
            return self

    stop = threading.Event()
    fatal = {"event": "error", "code": "403", "is_fatal": True, "message": "bad key"}
    ws = Rejecting([fatal], stop)
    record = _record()
    stt = _stt(settings, ws, record)
    stt.run_blocking(lambda timeout: None, stop)
    assert attempts == [1]
    assert any(e.startswith("error:") for e in record["events"])

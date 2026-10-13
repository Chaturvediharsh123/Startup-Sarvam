"""Tests for the Deepgram streaming STT client with a fake WebSocket (no network)."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any
from unittest import mock

import pytest
from websockets.exceptions import InvalidStatus

from core.config import Settings
from core.interfaces import SpeechToText
from stt.deepgram_realtime import DeepgramRealtimeSTT

KEY = "dg_test_key_123"


def results(text: str, *, is_final: bool, speech_final: bool = False,
            words: list[tuple[str, str]] | None = None) -> dict[str, Any]:
    alternative: dict[str, Any] = {"transcript": text, "confidence": 0.9}
    if words is not None:
        alternative["words"] = [{"word": w, "language": lang} for w, lang in words]
    return {"type": "Results", "is_final": is_final, "speech_final": speech_final,
            "channel": {"alternatives": [alternative]}}


class Recorder:
    def __init__(self) -> None:
        self.partial: list[str] = []
        self.final: list[tuple[str, str | None]] = []
        self.events: list[str] = []

    def stt(self, settings: Settings, **kwargs: Any) -> DeepgramRealtimeSTT:
        kwargs.setdefault("language", "multi")
        return DeepgramRealtimeSTT(
            KEY, settings, self.partial.append,
            lambda text, lang: self.final.append((text, lang)), self.events.append, **kwargs,
        )


class FakeDeepgram:
    """Async context manager + iterator that replays server messages, records what was sent."""

    def __init__(self, messages: list[dict[str, Any]], stop: threading.Event) -> None:
        self.messages = list(messages)
        self.sent: list[Any] = []
        self.stop = stop

    async def __aenter__(self) -> FakeDeepgram:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def __aiter__(self) -> FakeDeepgram:
        return self

    async def __anext__(self) -> str:
        if self.messages:
            await asyncio.sleep(0.02)
            return json.dumps(self.messages.pop(0))
        self.stop.set()
        await asyncio.sleep(0.3)
        raise StopAsyncIteration

    async def send(self, data: Any) -> None:
        self.sent.append(data)


def test_url_and_auth(settings: Settings) -> None:
    stop = threading.Event()
    ws = FakeDeepgram([], stop)
    seen: dict[str, Any] = {}

    def connect(url: str, additional_headers: dict[str, str]) -> FakeDeepgram:
        seen.update(url=url, headers=additional_headers)
        return ws

    Recorder().stt(settings, connect=connect).run_blocking(lambda t: None, stop)
    url = seen["url"]
    assert url.startswith("wss://api.deepgram.com/v1/listen?")
    for part in ("model=nova-3", "language=multi", "encoding=linear16", "sample_rate=16000",
                 "interim_results=true", "endpointing=800", "utterance_end_ms=1000",
                 "vad_events=true"):
        assert part in url, part
    assert seen["headers"] == {"Authorization": f"Token {KEY}"}
    assert KEY not in url


def test_session_streams_binary_audio_and_closes(settings: Settings) -> None:
    stop = threading.Event()
    ws = FakeDeepgram(
        [
            {"type": "Metadata", "request_id": "r1"},
            {"type": "SpeechStarted", "channel": [0], "timestamp": 0.1},
            results("Notepad", is_final=False),
            results("Notepad खोलो.", is_final=True, speech_final=True,
                    words=[("notepad", "en"), ("खोलो", "hi")]),
        ],
        stop,
    )
    chunks = [b"\x01\x00" * 1600, b"\x02\x00" * 1600]
    rec = Recorder()
    rec.stt(settings, connect=lambda url, additional_headers: ws).run_blocking(
        lambda timeout: chunks.pop(0) if chunks else None, stop
    )
    assert rec.partial == ["Notepad"]
    assert rec.final == [("Notepad खोलो.", "hi-IN")]  # tie goes to Hindi
    assert {"connected", "speech_start", "speech_end"} <= set(rec.events)
    assert ws.sent[0] == b"\x01\x00" * 1600 and isinstance(ws.sent[1], bytes)
    assert json.loads(ws.sent[-1]) == {"type": "CloseStream"}


def test_pieces_joined_until_speech_final(settings: Settings) -> None:
    rec = Recorder()
    stt = rec.stt(settings)
    stt.handle_message(results("YouTube पर", is_final=False))
    stt.handle_message(results("YouTube पर अरिजीत सिंह", is_final=True,
                               words=[("youtube", "en"), ("पर", "hi"), ("अरिजीत", "hi"),
                                      ("सिंह", "hi")]))
    stt.handle_message(results("के गाने", is_final=False))
    stt.handle_message(results("के गाने चलाओ.", is_final=True, speech_final=True,
                               words=[("के", "hi"), ("गाने", "hi"), ("चलाओ", "hi")]))
    assert rec.partial == ["YouTube पर", "YouTube पर अरिजीत सिंह",
                           "YouTube पर अरिजीत सिंह के गाने"]
    assert rec.final == [("YouTube पर अरिजीत सिंह के गाने चलाओ.", "hi-IN")]


def test_utterance_end_delivers_when_speech_final_missed(settings: Settings) -> None:
    rec = Recorder()
    stt = rec.stt(settings)
    stt.handle_message(results("Open Chrome.", is_final=True, words=[("open", "en"),
                                                                    ("chrome", "en")]))
    assert rec.final == []
    stt.handle_message({"type": "UtteranceEnd", "channel": [0], "last_word_end": 1.2})
    assert rec.final == [("Open Chrome.", "en-IN")]
    stt.handle_message({"type": "UtteranceEnd", "channel": [0], "last_word_end": 1.2})
    assert len(rec.final) == 1


def test_language_falls_back_to_segment_languages_then_hint(settings: Settings) -> None:
    rec = Recorder()
    stt = rec.stt(settings)
    message = results("namaste", is_final=True, speech_final=True)
    message["channel"]["alternatives"][0]["languages"] = ["hi"]
    stt.handle_message(message)
    stt.language_hint = "ta-IN"
    stt.handle_message(results("hmm", is_final=True, speech_final=True))
    assert rec.final == [("namaste", "hi-IN"), ("hmm", "ta-IN")]


def test_language_majority_wins(settings: Settings) -> None:
    rec = Recorder()
    rec.stt(settings).handle_message(results(
        "Set volume to forty percent.", is_final=True, speech_final=True,
        words=[("set", "en"), ("volume", "en"), ("to", "en"), ("forty", "en"), ("percent", "en")],
    ))
    assert rec.final == [("Set volume to forty percent.", "en-IN")]


def test_hindi_mode_detects_language_from_text(settings: Settings) -> None:
    rec = Recorder()
    stt = rec.stt(settings, language="hi")
    for text in ("Notepad खोलो.", "Open Chrome.", "Volume चालीस कर दो."):
        stt.handle_message(results(text, is_final=True, speech_final=True))
    assert rec.final == [("Notepad खोलो.", "hi-IN"), ("Open Chrome.", "en-IN"),
                         ("Volume चालीस कर दो.", "hi-IN")]


def test_default_language_is_hindi(settings: Settings) -> None:
    stt = DeepgramRealtimeSTT(KEY, settings, lambda t: None, lambda t, lang: None)
    assert stt.language == "hi" and "language=hi&" in stt.url()


def test_empty_speech_final_is_not_delivered(settings: Settings) -> None:
    rec = Recorder()
    stt = rec.stt(settings)
    stt.handle_message(results("", is_final=True, speech_final=True))
    stt.handle_message({"type": "UtteranceEnd", "channel": [0], "last_word_end": 0.0})
    assert rec.final == []
    assert "speech_end" not in rec.events  # silence is not an end of speech


def test_local_silence_final_drops_late_server_text(settings: Settings) -> None:
    rec = Recorder()
    stt = rec.stt(settings)
    stt.handle_message(results("Chrome kholo", is_final=False))
    silence = b"\x00\x00" * 1600  # 100 ms
    assert stt.local_silence_ms == 2300  # endpointing 800 + utterance_end 1000 + 500
    for _ in range(22):  # 2.2 s: Deepgram's own signals still have time, no local final
        assert stt._silence.feed(silence) is None
    text = stt._silence.feed(silence)  # 2.3 s
    assert text == "Chrome kholo"
    stt._local_final(text)
    assert rec.final == [("Chrome kholo", None)]
    assert "local_end_of_speech" in rec.events
    stt.handle_message(results("Chrome kholo.", is_final=True, speech_final=True))
    assert len(rec.final) == 1  # late server final for the same sentence is dropped
    stt.handle_message(results("Volume badhao.", is_final=True, speech_final=True))
    assert rec.final[-1] == ("Volume badhao.", None)


def test_speech_started_clears_late_window(settings: Settings) -> None:
    rec = Recorder()
    stt = rec.stt(settings)
    stt._local_final("Chrome kholo")
    stt.handle_message({"type": "SpeechStarted", "channel": [0], "timestamp": 2.0})
    stt.handle_message(results("Band karo.", is_final=True, speech_final=True))
    assert rec.final[-1] == ("Band karo.", None)


def test_bad_messages_ignored(settings: Settings) -> None:
    rec = Recorder()
    stt = rec.stt(settings)
    for message in ({"type": "Results"}, {"type": "Results", "channel": {"alternatives": []}},
                    {"type": "Error", "code": "x", "description": "meh"}, {"type": "Unknown"}):
        stt.handle_message(message)
    assert rec.final == [] and rec.partial == []


def test_reconnects_then_falls_back(settings: Settings) -> None:
    stop = threading.Event()
    attempts: list[int] = []

    class Dropping:
        async def __aenter__(self) -> Dropping:
            attempts.append(1)
            if len(attempts) >= 3:
                stop.set()
            raise OSError("network down")

        async def __aexit__(self, *exc: object) -> None:
            return None

    rec = Recorder()
    stt = rec.stt(settings, connect=lambda url, additional_headers: Dropping())
    stt.initial_backoff_s = 0.01
    stt.run_blocking(lambda timeout: None, stop)
    assert len(attempts) == 3
    assert rec.events.count("disconnected") == 3
    assert "fallback" in rec.events  # settings.stt_fallback_after defaults to 3
    assert not stt.health().ok


def test_rejected_key_stops_without_retry(settings: Settings) -> None:
    attempts: list[int] = []

    class Rejected:
        async def __aenter__(self) -> Rejected:
            attempts.append(1)
            raise InvalidStatus(mock.Mock(status_code=401))

        async def __aexit__(self, *exc: object) -> None:
            return None

    rec = Recorder()
    stt = rec.stt(settings, connect=lambda url, additional_headers: Rejected())
    stt.run_blocking(lambda timeout: None, threading.Event())
    assert len(attempts) == 1
    assert rec.events[-1] == "error:API key rejected (HTTP 401)"
    assert KEY not in stt.health().detail


def test_custom_endpointing_and_empty_key(settings: Settings) -> None:
    assert "endpointing=300" in Recorder().stt(settings, endpointing_ms=300).url()
    assert "language=hi&" in Recorder().stt(settings, language="hi").url()
    with pytest.raises(ValueError):
        DeepgramRealtimeSTT(" ", settings, lambda t: None, lambda t, lang: None)


def test_matches_protocol(settings: Settings) -> None:
    stt: SpeechToText = Recorder().stt(settings)
    assert callable(stt.run_blocking)

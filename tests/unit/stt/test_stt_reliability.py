"""Realtime STT reliability: batch fallback, local end-of-speech, language hint."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import httpx
import pytest

from core.config import Settings, load_settings
from sarvam.client import SarvamClient, SarvamError
from stt.batch_client import RestSTT
from stt.realtime_client import RealtimeSTT


def settings_with(**overrides: str) -> Settings:
    return load_settings(
        env_file=None, overrides={"SARVAM_API_KEY": "sk_test", **overrides}, settings_file=None
    )


class TimedWebSocket:
    """Fake socket that delivers ``(delay_s, message)`` pairs, then asks the client to stop."""

    def __init__(self, script: list[tuple[float, dict[str, Any]]], stop: threading.Event) -> None:
        self.script = list(script)
        self.stop = stop
        self.sent: list[dict[str, Any]] = []

    async def __aenter__(self) -> TimedWebSocket:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def __aiter__(self) -> TimedWebSocket:
        return self

    async def __anext__(self) -> str:
        if self.script:
            delay, message = self.script.pop(0)
            await asyncio.sleep(delay)
            return json.dumps(message)
        self.stop.set()
        await asyncio.sleep(0.3)
        raise StopAsyncIteration

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def close(self) -> None:
        return None


class Record:
    def __init__(self) -> None:
        self.partial: list[str] = []
        self.final: list[tuple[str, str | None]] = []
        self.events: list[str] = []
        self.urls: list[str] = []


def make_stt(settings: Settings, connect: Any, record: Record) -> RealtimeSTT:
    def wrapped(url: str, additional_headers: dict[str, str]) -> Any:
        record.urls.append(url)
        return connect()

    stt = RealtimeSTT(
        SarvamClient("sk_test"), settings, record.partial.append,
        lambda text, lang: record.final.append((text, lang)), record.events.append,
        connect=wrapped,
    )
    stt.initial_backoff_s = 0.01
    stt.max_backoff_s = 0.01
    return stt


def silent_reader(delay: float = 0.01) -> Any:
    def read(timeout: float) -> bytes:
        time.sleep(delay)
        return bytes(3200)  # 100 ms of silence at 16 kHz

    return read


# -- fallback to batch ---------------------------------------------------------------


def test_fallback_after_failed_attempts_then_reconnects() -> None:
    stop = threading.Event()
    attempts: list[int] = []
    record = Record()

    class Flaky:
        """Fails three times (network down), then connects and ends the script."""

        def __init__(self) -> None:
            attempts.append(1)
            self.ws = TimedWebSocket([(0.01, {"event": "session.begin"})], stop)

        async def __aenter__(self) -> TimedWebSocket:
            if len(attempts) <= 3:
                raise OSError("network down")
            return self.ws

        async def __aexit__(self, *exc: object) -> None:
            return None

    stt = make_stt(settings_with(STT_FALLBACK_AFTER="3"), Flaky, record)
    stt.run_blocking(lambda timeout: None, stop)
    assert record.events[:4] == ["disconnected", "disconnected", "disconnected", "fallback"]
    assert record.events.count("fallback") == 1  # announced once, retries continue quietly
    assert "connected" in record.events[4:]
    assert len(attempts) == 4
    assert not stt.in_fallback and stt.failures == 0


def test_health_reports_fallback() -> None:
    stop = threading.Event()
    attempts: list[int] = []

    class Down:
        async def __aenter__(self) -> None:
            attempts.append(1)
            if len(attempts) >= 2:
                stop.set()
            raise OSError("no route")

        async def __aexit__(self, *exc: object) -> None:
            return None

    record = Record()
    stt = make_stt(settings_with(STT_FALLBACK_AFTER="1"), Down, record)
    stt.run_blocking(lambda timeout: None, stop)
    assert record.events.count("fallback") == 1
    health = stt.health()
    assert not health.ok and "push-to-talk" in health.detail


# -- local end-of-speech -------------------------------------------------------------------


def test_local_silence_turns_partial_into_final_once() -> None:
    stop = threading.Event()
    script = [
        (0.0, {"event": "vad.speech_start", "utterance_idx": 0}),
        (0.0, {"event": "transcript.partial", "utterance_idx": 0, "text": "notepad kholo",
               "language": "hi-IN"}),
        # the server's end-of-speech arrives late; the local timeout already fired
        (0.6, {"event": "transcript.final", "utterance_idx": 0, "text": "notepad kholo"}),
        (0.0, {"event": "transcript.partial", "utterance_idx": 0, "text": "notepad"}),
    ]
    record = Record()
    stt = make_stt(settings_with(LOCAL_SILENCE_MS="700"), lambda: TimedWebSocket(script, stop),
                   record)
    stt.run_blocking(silent_reader(), stop)
    assert record.final == [("notepad kholo", "hi-IN")]
    assert record.partial == ["notepad kholo"]
    assert "local_end_of_speech" in record.events


def test_server_final_wins_when_in_time() -> None:
    stop = threading.Event()
    script = [
        (0.0, {"event": "transcript.partial", "utterance_idx": 0, "text": "volume"}),
        (0.0, {"event": "transcript.final", "utterance_idx": 0, "text": "volume badhao",
               "language": "hi-IN"}),
        (0.4, {"event": "session.begin"}),
    ]
    record = Record()
    stt = make_stt(settings_with(LOCAL_SILENCE_MS="900"), lambda: TimedWebSocket(script, stop),
                   record)
    stt.run_blocking(silent_reader(), stop)
    assert record.final == [("volume badhao", "hi-IN")]
    assert "local_end_of_speech" not in record.events


def test_speech_resets_local_timeout() -> None:
    stt = make_stt(settings_with(LOCAL_SILENCE_MS="300"), lambda: None, Record())
    stt.handle_message({"event": "transcript.partial", "utterance_idx": 1, "text": "hello"})
    loud = (b"\xff\x3f" + b"\x01\xc0") * 800  # +-16383, well above the silence level
    for _ in range(5):
        assert stt._silence.feed(loud) is None
    assert stt._silence.feed(bytes(3200)) is None
    assert stt._silence.feed(bytes(3200)) is None
    assert stt._silence.feed(bytes(3200)) == "hello"


# -- language hint ----------------------------------------------------------------------------


def test_language_hint_in_url_and_final() -> None:
    record = Record()
    stt = make_stt(settings_with(LANGUAGE_HINT="hi-IN"), lambda: None, record)
    assert "language_code=hi-IN" in stt.url()
    stt.handle_message({"event": "transcript.final", "text": "namaste"})
    assert record.final == [("namaste", "hi-IN")]
    stt.language_hint = "od-IN"
    assert "language_code=or-IN" in stt.url()  # realtime spells Odia or-IN
    stt.language_hint = ""
    assert "language_code=auto" in stt.url()


def test_rest_stt_language_hint() -> None:
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return httpx.Response(200, json={"transcript": "vanakkam", "language_code": None})

    client = SarvamClient("sk_test", transport=httpx.MockTransport(handler), sleep=lambda s: 0)
    stt = RestSTT(client, settings_with(LANGUAGE_HINT="ta-IN"))
    assert stt.transcribe(bytes(32000), 16000) == ("vanakkam", "ta-IN")
    assert b"ta-IN" in seen[0] and b"unknown" not in seen[0]
    stt.language_hint = ""
    stt.transcribe(bytes(32000), 16000)
    assert b"unknown" in seen[1]
    assert stt.health().ok


def test_rest_stt_health_after_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    client = SarvamClient("sk_test", transport=httpx.MockTransport(handler), sleep=lambda s: 0)
    stt = RestSTT(client, settings_with())
    with pytest.raises(SarvamError):
        stt.transcribe(bytes(32000), 16000)
    assert not stt.health().ok

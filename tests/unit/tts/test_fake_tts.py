"""Tests for the TTS fakes and for playback interrupts with a TTS stream."""

from __future__ import annotations

import sys
import threading
import time

import pytest

from audio.fake import FakeSpeaker
from audio.mic import MuteFlag
from core.interfaces import TextToSpeech
from tts.fake import MockTTS, SilentTTS


def test_mock_tts_prints_and_yields_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pyttsx3", None)  # offline voice not installed
    printed: list[str] = []
    tts = MockTTS(output=printed.append)
    assert list(tts.stream("Notepad khul gaya", "hi-IN")) == []
    assert printed == ["[voice] Notepad khul gaya"]
    assert tts.sample_rate == 24000


def test_silent_tts_duration_matches_text() -> None:
    tts = SilentTTS(24000, ms_per_char=50)
    text = "Notepad khul gaya hai abhi."  # 27 chars -> 1350 ms
    audio = b"".join(tts.stream(text, "hi-IN"))
    assert audio == bytes(len(audio))
    assert len(audio) == 1350 * 24 * 2
    assert tts.spoken == [(text, "hi-IN")]


def test_silent_tts_latency_and_realtime() -> None:
    tts = SilentTTS(16000, ms_per_char=10, latency_ms=100, realtime=True)
    started = time.monotonic()
    stream = tts.stream("x" * 30, None)  # 300 ms of audio
    next(stream)
    first = time.monotonic() - started
    list(stream)
    total = time.monotonic() - started
    assert 0.09 <= first < 0.3
    assert total >= 0.35


def test_silent_tts_cancel() -> None:
    tts = SilentTTS(16000, ms_per_char=100, realtime=True)
    stream = tts.stream("a long reply " * 10, None)
    next(stream)
    tts.cancel()
    assert list(stream) == []
    assert stream.join(1.0)


def test_interrupt_stops_playback_and_tts_fast() -> None:
    """Interrupt: the player stops in < 100 ms and the TTS stream is cancelled too."""
    mute = MuteFlag()
    events: list[object] = []
    speaker = FakeSpeaker(24000, mute, unmute_delay_s=0.25, realtime=True,
                          on_start=lambda: events.append("start"), on_finish=events.append)
    tts = SilentTTS(24000, ms_per_char=60, realtime=True)
    stream = tts.stream("Yeh ek lamba jawab hai jo bahut der tak chalega. " * 5, "hi-IN")
    result: list[bool] = []
    thread = threading.Thread(target=lambda: result.append(speaker.play(stream)))
    thread.start()
    time.sleep(0.3)
    assert mute.is_muted  # mic muted during the reply
    started = time.monotonic()
    speaker.stop()
    thread.join(1.0)
    assert time.monotonic() - started < 0.1
    assert result == [False]
    assert events == ["start", True]
    assert stream.cancelled and stream.join(1.0)
    assert mute.is_muted  # still muted for the echo tail
    time.sleep(0.3)
    assert not mute.is_muted


def test_fakes_match_protocol() -> None:
    def accept(tts: TextToSpeech) -> TextToSpeech:
        return tts

    accept(SilentTTS())
    accept(MockTTS(output=lambda s: None))

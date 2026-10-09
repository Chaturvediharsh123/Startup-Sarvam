"""Tests for the scripted STT fakes."""

from __future__ import annotations

import threading
import time

from core.interfaces import BatchSpeechToText, SpeechToText
from stt.fake import FakeBatchSTT, FakeSTT


def test_fake_stt_plays_script_then_waits_for_stop() -> None:
    partials: list[str] = []
    finals: list[tuple[str, str | None]] = []
    events: list[str] = []
    build = FakeSTT.factory([("notepad kholo", "hi-IN"), ("what time is it", "en-IN")],
                            delay_s=0.0)
    stt = build(partials.append, lambda t, lang: finals.append((t, lang)), events.append)
    stop = threading.Event()
    reads: list[float] = []

    def read(timeout: float) -> bytes | None:
        reads.append(timeout)
        time.sleep(0.01)
        return b"\x00\x00"

    thread = threading.Thread(target=stt.run_blocking, args=(read, stop))
    thread.start()
    time.sleep(0.2)
    assert thread.is_alive()  # keeps "listening" after the script, like a live socket
    stop.set()
    thread.join(1.0)
    assert not thread.is_alive()
    assert finals == [("notepad kholo", "hi-IN"), ("what time is it", "en-IN")]
    assert partials == ["notepad", "what", "what time", "what time is"]
    assert events[0] == "connected"
    assert events.count("speech_start") == events.count("speech_end") == 2
    assert reads and stt.audio_chunks > 0
    assert stt.health().ok


def test_fake_stt_stops_mid_script() -> None:
    finals: list[str] = []
    stt = FakeSTT([("a b c", None)] * 5, lambda t: None, lambda t, lang: finals.append(t),
                  delay_s=0.05)
    stop = threading.Event()
    thread = threading.Thread(target=stt.run_blocking, args=(lambda t: None, stop))
    thread.start()
    time.sleep(0.12)
    stop.set()
    thread.join(1.0)
    assert len(finals) < 5


def test_fake_batch_stt() -> None:
    stt = FakeBatchSTT([("haan", "hi-IN")], default=("", None))
    assert stt.transcribe(b"\x00\x00" * 10, 16000) == ("haan", "hi-IN")
    assert stt.transcribe(b"", 16000) == ("", None)
    assert stt.calls == [(20, 16000), (0, 16000)]


def test_fakes_match_protocols() -> None:
    def live(stt: SpeechToText) -> SpeechToText:
        return stt

    def batch(stt: BatchSpeechToText) -> BatchSpeechToText:
        return stt

    live(FakeSTT([], lambda t: None, lambda t, lang: None))
    batch(FakeBatchSTT())

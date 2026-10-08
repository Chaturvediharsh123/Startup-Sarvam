"""Tests for the local end-of-speech backup detector."""

from __future__ import annotations

import struct

from stt.vad import LocalSilenceDetector, chunk_level


def loud(samples: int = 1600) -> bytes:
    return struct.pack(f"<{samples}h", *([12000, -12000] * (samples // 2)))


def test_chunk_level() -> None:
    assert chunk_level(b"") == 0.0
    assert chunk_level(bytes(3200)) == 0.0
    assert chunk_level(loud()) == 1.0


def test_no_partial_never_fires() -> None:
    detector = LocalSilenceDetector(300)
    assert all(detector.feed(bytes(3200)) is None for _ in range(20))


def test_fires_once_after_silence() -> None:
    detector = LocalSilenceDetector(900)
    detector.on_partial("notepad kholo")
    results = [detector.feed(bytes(3200)) for _ in range(12)]  # 100 ms chunks
    assert results[:8] == [None] * 8
    assert results[8] == "notepad kholo"  # after 900 ms
    assert results[9:] == [None] * 3
    assert detector.pending == ""


def test_speech_and_new_words_reset_the_timer() -> None:
    detector = LocalSilenceDetector(300)
    detector.on_partial("volume")
    assert detector.feed_level(0.0, 200) is None
    assert detector.feed_level(0.5, 100) is None  # still talking
    assert detector.feed_level(0.0, 200) is None
    detector.on_partial("volume badhao")  # new words: the timer restarts
    assert detector.feed_level(0.0, 200) is None
    detector.on_partial("volume badhao")  # same text: no restart
    assert detector.feed_level(0.0, 100) == "volume badhao"


def test_server_final_cancels_backup() -> None:
    detector = LocalSilenceDetector(300)
    detector.on_partial("hello")
    detector.on_final()
    assert all(detector.feed_level(0.0, 100) is None for _ in range(10))


def test_threshold_follows_noise_gate() -> None:
    detector = LocalSilenceDetector(200, level_threshold=0.2)
    detector.on_partial("hi")
    assert detector.feed_level(0.1, 100) is None  # room noise below the gate counts as silence
    assert detector.feed_level(0.1, 100) == "hi"

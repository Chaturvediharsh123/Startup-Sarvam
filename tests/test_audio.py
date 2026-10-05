"""Tests for the mute flag, microphone callback, player and wake word (no sound devices)."""

from __future__ import annotations

import struct
import threading
from typing import Any

import pytest

from audio.mic import Microphone, MuteFlag, level_of, parse_device
from audio.player import AudioPlayer
from audio.wakeword import strip_wake_word, wake_variants


class FakeStream:
    """Records writes instead of playing audio."""

    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.started = self.stopped = self.aborted = self.closed = False

    def start(self) -> None:
        self.started = True

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def stop(self) -> None:
        self.stopped = True

    def abort(self) -> None:
        self.aborted = True

    def close(self) -> None:
        self.closed = True


def test_mute_flag_delayed_unmute() -> None:
    now = [0.0]
    flag = MuteFlag(clock=lambda: now[0])
    assert not flag.is_muted
    flag.mute()
    assert flag.is_muted
    flag.unmute(0.3)
    assert flag.is_muted
    now[0] = 0.31
    assert not flag.is_muted


def test_parse_device() -> None:
    assert parse_device("") is None
    assert parse_device(" 3 ") == 3
    assert parse_device("Headset Mic") == "Headset Mic"


def test_level_of() -> None:
    assert level_of(b"") == 0.0
    assert level_of(b"\x00\x00" * 100) == 0.0
    loud = struct.pack("<100h", *([20000, -20000] * 50))
    assert level_of(loud) == 1.0


def test_mic_callback_drops_chunks_while_muted() -> None:
    mute = MuteFlag()
    levels: list[float] = []
    mic = Microphone(16000, 100, mute, on_level=levels.append)
    assert mic.blocksize == 1600
    mic._callback(b"\x01\x00" * 1600, 1600, None, None)
    mute.mute()
    mic._callback(b"\x02\x00" * 1600, 1600, None, None)
    assert mic.read(0.01) == b"\x01\x00" * 1600
    assert mic.read(0.01) is None
    assert len(levels) == 1


def test_mic_queue_full_does_not_raise() -> None:
    mic = Microphone(16000, 100, MuteFlag(), max_queue=1)
    mic._callback(b"a\x00", 1, None, None)
    mic._callback(b"b\x00", 1, None, None)
    mic.drain()
    assert mic.read(0.01) is None


def test_player_plays_and_mutes_mic() -> None:
    mute = MuteFlag()
    stream = FakeStream()
    seen_muted: list[bool] = []
    first: list[int] = []

    def chunks() -> Any:
        seen_muted.append(mute.is_muted)
        yield b"\x00\x00" * 2400  # 100 ms at 24 kHz -> two 50 ms slices
        yield b"\x01\x00" * 100

    player = AudioPlayer(24000, mute, unmute_delay_s=0.0, stream_factory=lambda sr: stream)
    assert player.play(chunks(), on_first_audio=lambda: first.append(1)) is True
    assert seen_muted == [True]
    assert first == [1]
    assert len(stream.written) == 3
    assert stream.started and stream.stopped and stream.closed
    assert not player.is_speaking
    assert not mute.is_muted


def test_player_stop_interrupts() -> None:
    stream = FakeStream()
    player = AudioPlayer(24000, stream_factory=lambda sr: stream)
    produced: list[int] = []

    def chunks() -> Any:
        for i in range(100):
            produced.append(i)
            if i == 2:
                player.stop()
            yield b"\x00\x00" * 100

    assert player.play(chunks()) is False
    assert len(produced) == 3
    assert stream.aborted


def test_player_no_audio_never_opens_stream() -> None:
    opened: list[int] = []
    player = AudioPlayer(24000, stream_factory=lambda sr: opened.append(sr) or FakeStream())
    assert player.play(iter(())) is True
    assert opened == []


def test_player_is_speaking_during_play() -> None:
    gate = threading.Event()
    states: list[bool] = []
    player = AudioPlayer(24000, stream_factory=lambda sr: FakeStream())

    def chunks() -> Any:
        states.append(player.is_speaking)
        gate.set()
        yield b"\x00\x00"

    player.play(chunks())
    assert states == [True]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Sarvam, notepad kholo", "notepad kholo"),
        ("hey sarvam Volume 30 kar do", "Volume 30 kar do"),
        ("सर्वम नोटपैड खोलो", "नोटपैड खोलो"),
        ("ok सर्वम्, क्रोम खोलो।", "क्रोम खोलो।"),
        ("Sarvam", ""),
        ("notepad kholo", None),
        ("the sarvam app", None),
    ],
)
def test_wake_word(text: str, expected: str | None) -> None:
    assert strip_wake_word(text, "sarvam") == expected


def test_wake_word_off_passes_through() -> None:
    assert strip_wake_word("Notepad kholo", "") == "Notepad kholo"


def test_wake_variants_custom_list() -> None:
    variants = wake_variants("Dost, दोस्त")
    assert {"dost", "दोस्त"} <= variants
    assert strip_wake_word("दोस्त गाना चलाओ", "dost,दोस्त") == "गाना चलाओ"

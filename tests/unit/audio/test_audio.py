"""Tests for the mute flag, microphone callback and player (no sound devices)."""

from __future__ import annotations

import struct
import threading
import time
from typing import Any

from audio.mic import Microphone, MuteFlag, level_of, parse_device
from audio.speaker import AudioPlayer


class FakeStream:
    """Records writes instead of playing audio; ``write_s`` simulates a blocking device."""

    def __init__(self, write_s: float = 0.0) -> None:
        self.written: list[bytes] = []
        self.write_s = write_s
        self.started = self.stopped = self.aborted = self.closed = False

    def start(self) -> None:
        self.started = True

    def write(self, data: bytes) -> None:
        self.written.append(data)
        if self.write_s:
            time.sleep(self.write_s)

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
    assert mic.dropped == 1
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


def test_player_keeps_mic_muted_for_unmute_delay() -> None:
    mute = MuteFlag()
    player = AudioPlayer(24000, mute, unmute_delay_s=0.25, stream_factory=lambda sr: FakeStream())
    player.play(iter([b"\x00\x00" * 10]))
    assert mute.is_muted  # echo tail protection
    time.sleep(0.3)
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
    finished: list[bool] = []
    player = AudioPlayer(24000, stream_factory=lambda sr: opened.append(sr) or FakeStream(),
                         on_finish=finished.append)
    assert player.play(iter(())) is True
    assert opened == []
    assert finished == []  # no SpeakStarted, so no SpeakFinished either


def test_player_is_speaking_during_play() -> None:
    states: list[bool] = []
    player = AudioPlayer(24000, stream_factory=lambda sr: FakeStream())

    def chunks() -> Any:
        states.append(player.is_speaking)
        yield b"\x00\x00"

    player.play(chunks())
    assert states == [True]


def test_player_start_and_finish_callbacks() -> None:
    events: list[Any] = []
    player = AudioPlayer(24000, stream_factory=lambda sr: FakeStream(),
                         on_start=lambda: events.append("start"),
                         on_finish=lambda interrupted: events.append(("finish", interrupted)))
    player.play(iter([b"\x00\x00" * 10, b"\x00\x00" * 10]))
    assert events == ["start", ("finish", False)]


def test_player_closes_source_generator() -> None:
    closed: list[bool] = []

    def chunks() -> Any:
        try:
            while True:
                yield b"\x00\x00" * 100
        finally:
            closed.append(True)

    player = AudioPlayer(24000, stream_factory=lambda sr: FakeStream())
    source = chunks()

    def stop_soon() -> None:
        time.sleep(0.05)
        player.stop()

    threading.Thread(target=stop_soon).start()
    assert player.play(source) is False
    assert closed == [True]  # TTS prefetch behind the generator is told to stop


def test_player_stop_within_100ms() -> None:
    """Interrupt stops playback in < 100 ms even with a blocking 50 ms device write."""
    finished: list[bool] = []
    player = AudioPlayer(24000, stream_factory=lambda sr: FakeStream(write_s=0.05),
                         on_finish=finished.append)

    def endless() -> Any:
        while True:
            yield b"\x00\x00" * 2400

    result: list[bool] = []
    thread = threading.Thread(target=lambda: result.append(player.play(endless())))
    thread.start()
    time.sleep(0.3)
    assert player.is_speaking
    started = time.monotonic()
    player.stop()
    thread.join(1.0)
    elapsed = time.monotonic() - started
    assert result == [False]
    assert elapsed < 0.1
    assert finished == [True]


def test_player_stop_cancels_source_waiting_for_network() -> None:
    """A source with ``cancel()`` (a TTS stream) is cancelled even while blocked."""

    class BlockingSource:
        def __init__(self) -> None:
            self.cancelled = threading.Event()
            self.sent = False

        def __iter__(self) -> BlockingSource:
            return self

        def __next__(self) -> bytes:
            if not self.sent:
                self.sent = True
                return b"\x00\x00" * 100
            self.cancelled.wait(5.0)  # "waiting for the next TTS chunk"
            raise StopIteration

        def cancel(self) -> None:
            self.cancelled.set()

    source = BlockingSource()
    player = AudioPlayer(24000, stream_factory=lambda sr: FakeStream())
    result: list[bool] = []
    thread = threading.Thread(target=lambda: result.append(player.play(source)))
    thread.start()
    time.sleep(0.1)
    started = time.monotonic()
    player.stop()
    thread.join(1.0)
    assert result == [False]
    assert time.monotonic() - started < 0.1
    assert source.cancelled.is_set()

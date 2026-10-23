"""Fakes for WS1: a microphone that "speaks" WAV files and a speaker that records.

* :class:`FakeMicrophone` has the same interface as :class:`audio.mic.Microphone`
  (``start``/``stop``/``read``/``drain``/``mute``/``on_level``/``health``). It replays
  WAV files (default: ``tests/audio_samples``) in 100 ms chunks, honours the mute
  flag and the noise gate, and can run in real time or as fast as possible.
* :class:`FakeSpeaker` is an :class:`audio.speaker.AudioPlayer` whose output stream
  records chunks instead of playing them, optionally taking real-time duration.

Neither touches a sound device, so the whole pipeline runs in tests and CI.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import wave
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from audio.mic import MuteFlag, level_of
from audio.resample import resample_int16, to_mono_int16
from audio.speaker import AudioPlayer
from core.config import Settings
from core.interfaces import Health

logger = logging.getLogger(__name__)

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "tests" / "audio_samples"


def read_wav(path: str | Path, sample_rate: int = 16000) -> bytes:
    """Load a 16-bit WAV file as mono PCM at ``sample_rate`` Hz."""
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise ValueError(f"{path}: only 16-bit WAV files are supported")
        pcm = to_mono_int16(wav.readframes(wav.getnframes()), wav.getnchannels())
        rate = wav.getframerate()
    return resample_int16(pcm, rate, sample_rate)


class FakeMicrophone:
    """Replays WAV files as if someone were speaking into the microphone.

    Args:
        files: WAV paths; ``None`` = every ``*.wav`` in :data:`SAMPLES_DIR`, sorted.
        sample_rate: output rate (16000).
        chunk_ms: chunk length (100).
        mute: shared mute flag; chunks are dropped while it is set, like the real mic.
        on_level: optional callback with each chunk's 0-1 level.
        noise_gate: chunks below this level are zeroed but still queued.
        realtime: pace chunks at ``chunk_ms`` (True) or push them as fast as read.
        loop: start again from the first file when the last one ends.
        gap_ms: silence inserted between files.
        trailing_silence: in real-time mode, keep sending silence after the files
            (like a quiet room) until :meth:`stop`.
    """

    def __init__(
        self,
        files: Sequence[str | Path] | None = None,
        sample_rate: int = 16000,
        chunk_ms: int = 100,
        mute: MuteFlag | None = None,
        on_level: Callable[[float], None] | None = None,
        *,
        noise_gate: float = 0.0,
        realtime: bool = True,
        loop: bool = False,
        gap_ms: int = 500,
        trailing_silence: bool = True,
        max_queue: int = 1000,
    ) -> None:
        self.files = [Path(f) for f in files] if files is not None else None
        self.sample_rate = sample_rate
        self.device_rate = sample_rate
        self.chunk_ms = chunk_ms
        self.blocksize = sample_rate * chunk_ms // 1000
        self.mute = mute or MuteFlag()
        self.on_level = on_level
        self.noise_gate = noise_gate
        self.realtime = realtime
        self.loop = loop
        self.gap_ms = gap_ms
        self.trailing_silence = trailing_silence
        self.on_error: Callable[[str], None] | None = None
        self.on_recovered: Callable[[], None] | None = None
        self.chunks: queue.Queue[bytes] = queue.Queue(maxsize=max_queue)
        self.finished = threading.Event()  # set once every file was played
        self.sent = 0
        self.dropped = 0
        self.error: str | None = None
        self._closing = threading.Event()
        self._thread: threading.Thread | None = None

    @classmethod
    def from_settings(
        cls, settings: Settings, mute: MuteFlag, **kwargs: Any
    ) -> FakeMicrophone:
        """Same constructor shape as :meth:`audio.mic.Microphone.from_settings`."""
        return cls(sample_rate=settings.sample_rate, chunk_ms=settings.chunk_ms, mute=mute,
                   noise_gate=settings.noise_gate, **kwargs)

    def _paths(self) -> list[Path]:
        paths = self.files if self.files is not None else sorted(SAMPLES_DIR.glob("*.wav"))
        if not paths:
            raise RuntimeError(f"Fake microphone: no WAV files found in {SAMPLES_DIR}")
        return paths

    def _audio(self) -> bytes:
        """All files joined with ``gap_ms`` of silence, padded to whole chunks."""
        gap = bytes(self.sample_rate * self.gap_ms // 1000 * 2)
        try:
            pcm = gap.join(read_wav(p, self.sample_rate) for p in self._paths())
        except (OSError, ValueError, wave.Error) as exc:
            raise RuntimeError(f"Fake microphone could not read samples: {exc}") from exc
        size = self.blocksize * 2
        return pcm + bytes(-len(pcm) % size)

    def start(self) -> None:
        """Start "speaking". Raises ``RuntimeError`` if the sample files are missing."""
        if self._thread is not None:
            return
        audio = self._audio()
        self._closing.clear()
        self.finished.clear()
        self._thread = threading.Thread(target=self._run, args=(audio,), name="fake-mic",
                                        daemon=True)
        self._thread.start()

    def _run(self, audio: bytes) -> None:
        size = self.blocksize * 2
        silence = bytes(size)
        next_at = time.monotonic()
        while not self._closing.is_set():
            for start in range(0, len(audio), size):
                if not self._emit(audio[start : start + size], next_at):
                    return
                next_at += self.chunk_ms / 1000
            if not self.loop:
                break
        self.finished.set()
        while self.realtime and self.trailing_silence and self._emit(silence, next_at):
            next_at += self.chunk_ms / 1000

    def _emit(self, chunk: bytes, due: float) -> bool:
        """Wait until ``due`` (real-time mode), then queue one chunk. False when stopping."""
        if self.realtime and self._closing.wait(max(0.0, due - time.monotonic())):
            return False
        if self._closing.is_set():
            return False
        if self.mute.is_muted:
            return True
        level = level_of(chunk)
        if self.on_level is not None:
            self.on_level(level)
        if level < self.noise_gate:
            chunk = bytes(len(chunk))
        while not self._closing.is_set():
            try:
                self.chunks.put(chunk, timeout=0.1)
                self.sent += 1
                return True
            except queue.Full:
                continue
        return False

    def stop(self) -> None:
        """Stop the replay thread and empty the queue."""
        self._closing.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self.drain()

    def drain(self) -> None:
        """Throw away queued audio."""
        while True:
            try:
                self.chunks.get_nowait()
            except queue.Empty:
                return

    def read(self, timeout: float = 0.5) -> bytes | None:
        """Next chunk, or ``None`` after ``timeout`` seconds."""
        try:
            return self.chunks.get(timeout=timeout)
        except queue.Empty:
            return None

    @property
    def running(self) -> bool:
        """True between :meth:`start` and :meth:`stop`."""
        return self._thread is not None

    def health(self) -> Health:
        """Always healthy; says it is a fake."""
        return Health("audio", True, "fake mic" + (" on" if self.running else " off"))


class _RecordingStream:
    """Output stream stand-in that records writes (and can take real time)."""

    def __init__(self, speaker: FakeSpeaker) -> None:
        self.speaker = speaker

    def start(self) -> None:
        self.speaker.streams_opened += 1

    def write(self, data: bytes) -> None:
        self.speaker.played.append(bytes(data))
        if self.speaker.realtime:
            self.speaker._stop.wait(len(data) / 2 / self.speaker.sample_rate)

    def stop(self) -> None:
        return None

    def abort(self) -> None:
        self.speaker.aborted += 1

    def close(self) -> None:
        return None


class FakeSpeaker(AudioPlayer):
    """An :class:`AudioPlayer` that records audio instead of playing it.

    Same mute, ``on_start``/``on_finish`` and ``stop()`` behaviour as the real player.

    Args:
        realtime: make each write take as long as the audio would (for timing tests).
    """

    def __init__(
        self,
        sample_rate: int = 24000,
        mute: MuteFlag | None = None,
        unmute_delay_s: float = 0.3,
        *,
        realtime: bool = False,
        on_start: Callable[[], None] | None = None,
        on_finish: Callable[[bool], None] | None = None,
    ) -> None:
        super().__init__(sample_rate, mute, unmute_delay_s, self._open_stream,
                         on_start=on_start, on_finish=on_finish)
        self.realtime = realtime
        self.played: list[bytes] = []
        self.streams_opened = 0
        self.aborted = 0

    def _open_stream(self, sample_rate: int) -> _RecordingStream:
        return _RecordingStream(self)

    @property
    def audio(self) -> bytes:
        """Everything written so far."""
        return b"".join(self.played)

    @property
    def seconds_played(self) -> float:
        """Duration of everything written so far."""
        return len(self.audio) / 2 / self.sample_rate

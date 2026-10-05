"""Microphone capture: 16-bit mono PCM chunks pushed into a queue.

While the assistant speaks, :class:`MuteFlag` is set and chunks are dropped so
the assistant does not hear itself.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


class MuteFlag:
    """Thread-safe "mic is muted" flag with a delayed unmute."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._muted = False
        self._until = 0.0

    def mute(self) -> None:
        """Mute now."""
        with self._lock:
            self._muted = True

    def unmute(self, delay_s: float = 0.0) -> None:
        """Unmute, optionally only after ``delay_s`` seconds."""
        with self._lock:
            self._muted = False
            self._until = self._clock() + delay_s

    @property
    def is_muted(self) -> bool:
        """True while muted or during the unmute delay."""
        with self._lock:
            return self._muted or self._clock() < self._until


def parse_device(value: str) -> int | str | None:
    """Config ``MIC_DEVICE``: empty = default device, digits = index, else a name."""
    value = value.strip()
    if not value:
        return None
    return int(value) if value.isdigit() else value


def level_of(chunk: bytes) -> float:
    """Loudness of a 16-bit PCM chunk from 0.0 to 1.0 (RMS), for the UI meter."""
    if len(chunk) < 2:
        return 0.0
    samples = np.frombuffer(chunk[: len(chunk) - len(chunk) % 2], dtype=np.int16)
    rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
    return min(1.0, rms / 8000.0)


class Microphone:
    """Captures audio with ``sounddevice`` into :attr:`chunks`.

    Args:
        sample_rate: 16000 (Sarvam realtime STT accepts 8000 or 16000 only).
        chunk_ms: chunk length; 100 ms = 3200 bytes at 16 kHz.
        mute: shared flag; chunks are dropped while it is set.
        device: index or name from ``MIC_DEVICE``; ``None`` = system default.
        on_level: optional callback with the 0–1 level of each chunk.
    """

    def __init__(
        self,
        sample_rate: int,
        chunk_ms: int,
        mute: MuteFlag,
        device: int | str | None = None,
        on_level: Callable[[float], None] | None = None,
        max_queue: int = 100,
    ) -> None:
        self.sample_rate = sample_rate
        self.blocksize = sample_rate * chunk_ms // 1000
        self.mute = mute
        self.device = device
        self.on_level = on_level
        self.chunks: queue.Queue[bytes] = queue.Queue(maxsize=max_queue)
        self._stream: Any = None

    def _callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        """sounddevice audio-thread callback: only touches the queue."""
        if status:
            logger.debug("Mic status: %s", status)
        if self.mute.is_muted:
            return
        chunk = bytes(indata)
        if self.on_level is not None:
            self.on_level(level_of(chunk))
        try:
            self.chunks.put_nowait(chunk)
        except queue.Full:
            logger.warning("Mic queue full; dropping audio")

    def start(self) -> None:
        """Open the input stream. Raises ``RuntimeError`` if no mic is available."""
        if self._stream is not None:
            return
        import sounddevice as sd

        try:
            self._stream = sd.RawInputStream(
                samplerate=self.sample_rate,
                blocksize=self.blocksize,
                channels=1,
                dtype="int16",
                device=self.device,
                callback=self._callback,
            )
            self._stream.start()
        except (sd.PortAudioError, ValueError) as exc:
            self._stream = None
            raise RuntimeError(f"Microphone not available: {exc}") from exc
        logger.info("Microphone started (%d Hz, device=%s)", self.sample_rate, self.device)

    def stop(self) -> None:
        """Close the input stream and empty the queue."""
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001 - closing must not raise during shutdown
                logger.exception("Error closing microphone")
        self.drain()

    def drain(self) -> None:
        """Throw away queued audio (e.g. after the assistant spoke)."""
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
        """True while the stream is open."""
        return self._stream is not None

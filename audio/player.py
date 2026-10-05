"""Speaker playback that starts as soon as the first TTS chunk arrives.

Chunks are written to a ``sounddevice`` output stream in small slices, so
:meth:`AudioPlayer.stop` takes effect within ~50 ms. The shared mic
:class:`~audio.mic.MuteFlag` is set while speaking and cleared shortly after.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from typing import Any

from audio.mic import MuteFlag

logger = logging.getLogger(__name__)

StreamFactory = Callable[[int], Any]


def _default_stream(sample_rate: int) -> Any:
    import sounddevice as sd

    return sd.RawOutputStream(samplerate=sample_rate, channels=1, dtype="int16")


class AudioPlayer:
    """Plays 16-bit mono PCM chunks as they arrive.

    Args:
        sample_rate: playback rate; must match the TTS output (24000 for bulbul:v3).
        mute: mic mute flag, set while speaking.
        unmute_delay_s: keep the mic muted this long after playback ends (echo tail).
        stream_factory: creates the output stream (tests pass a fake).
    """

    def __init__(
        self,
        sample_rate: int,
        mute: MuteFlag | None = None,
        unmute_delay_s: float = 0.3,
        stream_factory: StreamFactory = _default_stream,
    ) -> None:
        self.sample_rate = sample_rate
        self.mute = mute
        self.unmute_delay_s = unmute_delay_s
        self._factory = stream_factory
        self._stop = threading.Event()
        self._speaking = threading.Event()
        self._slice = sample_rate // 20 * 2  # 50 ms of int16 audio

    @property
    def is_speaking(self) -> bool:
        """True while audio is playing."""
        return self._speaking.is_set()

    def stop(self) -> None:
        """Interrupt the current playback (F9 / Stop button / barge-in)."""
        if self._speaking.is_set():
            logger.info("Playback stopped by user")
        self._stop.set()

    def play(
        self, chunks: Iterable[bytes], on_first_audio: Callable[[], None] | None = None
    ) -> bool:
        """Play every chunk; returns False if :meth:`stop` interrupted playback.

        Args:
            chunks: PCM chunks, usually a generator still downloading from TTS.
            on_first_audio: called once when the first audio is written (latency metric).
        """
        self._stop.clear()
        self._speaking.set()
        if self.mute is not None:
            self.mute.mute()
        stream = None
        try:
            for chunk in chunks:
                if self._stop.is_set():
                    return False
                if stream is None:
                    stream = self._factory(self.sample_rate)
                    stream.start()
                    if on_first_audio is not None:
                        on_first_audio()
                if not self._write(stream, chunk):
                    return False
            return not self._stop.is_set()
        finally:
            self._finish(stream)

    def _write(self, stream: Any, chunk: bytes) -> bool:
        """Write one chunk in 50 ms slices, checking for stop between slices."""
        for start in range(0, len(chunk), self._slice):
            if self._stop.is_set():
                return False
            stream.write(chunk[start : start + self._slice])
        return True

    def _finish(self, stream: Any) -> None:
        if stream is not None:
            try:
                if self._stop.is_set():
                    stream.abort()
                else:
                    stream.stop()  # waits for buffered audio to finish
                stream.close()
            except Exception:  # noqa: BLE001 - audio teardown must not crash the turn
                logger.exception("Error closing output stream")
        self._speaking.clear()
        if self.mute is not None:
            self.mute.unmute(self.unmute_delay_s)

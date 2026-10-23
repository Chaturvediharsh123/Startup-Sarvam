"""Speaker playback that starts as soon as the first TTS chunk arrives.

Chunks are written to a ``sounddevice`` output stream in 50 ms slices, so
:meth:`AudioPlayer.stop` takes effect within ~50 ms. Stopping also cancels and
closes the chunk source, so a TTS stream stops downloading the next sentence.

The shared mic :class:`~audio.mic.MuteFlag` is set for the whole reply and cleared
``unmute_delay_s`` (default 0.3 s) after playback ends, so the mic never hears the
assistant. ``on_start`` / ``on_finish`` map to the ``SpeakStarted`` /
``SpeakFinished`` events.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from typing import Any

from audio.mic import MuteFlag
from core.interfaces import Health

logger = logging.getLogger(__name__)

StreamFactory = Callable[[int], Any]


def _default_stream(sample_rate: int, device: int | str | None = None) -> Any:
    try:
        import sounddevice as sd
    except (ImportError, OSError) as exc:
        raise RuntimeError(
            f"Speaker not available: the sound library could not load ({exc})"
        ) from exc
    try:
        return sd.RawOutputStream(samplerate=sample_rate, channels=1, dtype="int16",
                                  device=device)
    except (sd.PortAudioError, ValueError, OSError) as exc:
        raise RuntimeError(
            f"Speaker not available: plug in speakers or headphones ({exc})"
        ) from exc


def stream_for_device(device: int | str | None) -> StreamFactory:
    """A stream factory for a chosen output device (``SPEAKER_DEVICE``; None = default)."""
    return lambda sample_rate: _default_stream(sample_rate, device)


class AudioPlayer:
    """Plays 16-bit mono PCM chunks as they arrive.

    Args:
        sample_rate: playback rate; must match the TTS output (24000 for bulbul:v3).
        mute: mic mute flag, set while speaking.
        unmute_delay_s: keep the mic muted this long after playback ends (echo tail).
        stream_factory: creates the output stream (tests pass a fake).
        on_start: called once when the first audio of a reply is written
            (publish ``SpeakStarted``).
        on_finish: called with ``interrupted`` when a reply that started has ended
            (publish ``SpeakFinished``). Not called if no audio was ever played.
    """

    def __init__(
        self,
        sample_rate: int,
        mute: MuteFlag | None = None,
        unmute_delay_s: float = 0.3,
        stream_factory: StreamFactory = _default_stream,
        *,
        on_start: Callable[[], None] | None = None,
        on_finish: Callable[[bool], None] | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.mute = mute
        self.unmute_delay_s = unmute_delay_s
        self.on_start = on_start
        self.on_finish = on_finish
        self._factory = stream_factory
        self._stop = threading.Event()
        self._speaking = threading.Event()
        self._slice = sample_rate // 20 * 2  # 50 ms of int16 audio
        self._source: Any = None

    @property
    def is_speaking(self) -> bool:
        """True while a reply is being played."""
        return self._speaking.is_set()

    def stop(self) -> None:
        """Interrupt the current playback (F9 / Stop button / barge-in).

        Thread-safe. Playback halts within one 50 ms slice; a source with a
        ``cancel()`` method (a TTS stream) is cancelled right away, so a wait for
        the next network chunk ends too.
        """
        if self._speaking.is_set():
            logger.info("Playback stopped by user")
        self._stop.set()
        cancel = getattr(self._source, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:  # noqa: BLE001 - stopping must never raise
                logger.debug("Source cancel failed", exc_info=True)

    def play(
        self, chunks: Iterable[bytes], on_first_audio: Callable[[], None] | None = None
    ) -> bool:
        """Play every chunk; returns False if :meth:`stop` interrupted playback.

        Args:
            chunks: PCM chunks, usually a TTS stream still downloading.
            on_first_audio: called once when the first audio is written (latency metric).

        Raises:
            RuntimeError: if no output device can be opened.
        """
        self._stop.clear()
        self._speaking.set()
        self._source = chunks
        if self.mute is not None:
            self.mute.mute()
        stream = None
        try:
            for chunk in chunks:
                if self._stop.is_set():
                    return False
                if not chunk:
                    continue
                if stream is None:
                    stream = self._factory(self.sample_rate)
                    stream.start()
                    if on_first_audio is not None:
                        on_first_audio()
                    _notify(self.on_start)
                if not self._write(stream, chunk):
                    return False
            return not self._stop.is_set()
        finally:
            self._finish(stream, chunks)

    def _write(self, stream: Any, chunk: bytes) -> bool:
        """Write one chunk in 50 ms slices, checking for stop between slices."""
        for start in range(0, len(chunk), self._slice):
            if self._stop.is_set():
                return False
            stream.write(chunk[start : start + self._slice])
        return True

    def _finish(self, stream: Any, chunks: Iterable[bytes]) -> None:
        interrupted = self._stop.is_set()
        if stream is not None:
            try:
                if interrupted:
                    stream.abort()
                else:
                    stream.stop()  # waits for buffered audio to finish
                stream.close()
            except Exception:  # noqa: BLE001 - audio teardown must not crash the turn
                logger.exception("Error closing output stream")
        close = getattr(chunks, "close", None)
        if callable(close):
            try:
                close()  # stops TTS prefetch / closes the HTTP response
            except Exception:  # noqa: BLE001 - the source is someone else's code
                logger.debug("Closing the audio source failed", exc_info=True)
        self._source = None
        self._speaking.clear()
        if self.mute is not None:
            self.mute.unmute(self.unmute_delay_s)
        if stream is not None and self.on_finish is not None:
            on_finish = self.on_finish
            _notify(lambda: on_finish(interrupted))

    def health(self) -> Health:
        """Status-bar health for the speaker."""
        return Health("speaker", True, "speaking" if self.is_speaking else "idle")


def _notify(callback: Callable[[], Any] | None) -> None:
    if callback is None:
        return
    try:
        callback()
    except Exception:  # noqa: BLE001 - a listener bug must not break playback
        logger.exception("Speaker callback failed")

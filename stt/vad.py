"""Local end-of-speech backup for the realtime STT client.

Sarvam's server VAD normally ends each utterance with ``transcript.final`` after
``vad_silence_ms`` of silence. If that signal goes missing (packet loss, a stuck
utterance), :class:`LocalSilenceDetector` notices that a partial transcript was
seen and the mic has then been quiet for ``local_silence_ms`` (700-900 ms), and
hands back the last partial so the client can use it as the final sentence.
"""

from __future__ import annotations

import numpy as np

SILENCE_LEVEL = 0.015
"""Chunk level (0-1 RMS, same scale as the UI meter) below which audio counts as silence."""


def chunk_level(chunk: bytes) -> float:
    """Loudness of a 16-bit PCM chunk from 0.0 to 1.0 (RMS / 8000, like ``audio.mic``)."""
    if len(chunk) < 2:
        return 0.0
    samples = np.frombuffer(chunk[: len(chunk) - len(chunk) % 2], dtype=np.int16)
    rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
    return min(1.0, rms / 8000.0)


class LocalSilenceDetector:
    """Turns "partial seen, then silence" into a backup end-of-speech.

    Feed it every mic chunk (:meth:`feed`) and every partial transcript
    (:meth:`on_partial`); call :meth:`on_final` when the server's final arrives.
    :meth:`feed` returns the pending partial text once ``silence_ms`` of quiet audio
    has followed it, then forgets it (so it fires at most once per utterance).

    Args:
        silence_ms: quiet time after the last partial that ends the utterance.
        level_threshold: chunk level below which audio counts as silence.
        sample_rate: rate of the fed PCM, used to measure chunk duration.
    """

    def __init__(
        self, silence_ms: int, level_threshold: float = SILENCE_LEVEL, sample_rate: int = 16000
    ) -> None:
        self.silence_ms = silence_ms
        self.level_threshold = level_threshold
        self.sample_rate = sample_rate
        self._partial = ""
        self._silent_ms = 0.0

    @property
    def pending(self) -> str:
        """The partial text waiting for a final (empty when none)."""
        return self._partial

    def reset(self) -> None:
        """Forget the pending partial and the silence so far."""
        self._partial = ""
        self._silent_ms = 0.0

    def on_partial(self, text: str) -> None:
        """A partial arrived; new words mean the user was still speaking."""
        text = text.strip()
        if text and text != self._partial:
            self._partial = text
            self._silent_ms = 0.0

    def on_final(self) -> None:
        """The server ended the utterance itself; nothing to back up."""
        self.reset()

    def feed_level(self, level: float, duration_ms: float) -> str | None:
        """Account for one chunk of ``level`` lasting ``duration_ms``.

        Returns the pending partial when the silence timeout is reached, else ``None``.
        """
        if level >= self.level_threshold:
            self._silent_ms = 0.0
            return None
        if not self._partial:
            return None
        self._silent_ms += duration_ms
        if self._silent_ms < self.silence_ms:
            return None
        text = self._partial
        self.reset()
        return text

    def feed(self, chunk: bytes) -> str | None:
        """Like :meth:`feed_level` for a raw 16-bit mono PCM chunk."""
        duration_ms = len(chunk) / 2 / self.sample_rate * 1000
        return self.feed_level(chunk_level(chunk), duration_ms)

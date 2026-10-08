"""Fakes for WS6: text-to-speech without Sarvam.

* :class:`MockTTS` prints the reply and, if ``pyttsx3`` is installed, speaks it with
  the offline Windows voice (used by ``python main.py --mock``). It yields no PCM.
* :class:`SilentTTS` yields silent PCM whose length matches how long the reply
  would take to say (``ms_per_char``), optionally with a first-chunk latency and
  real-time pacing, so playback, mute timing and interrupts can be tested.

Nothing in this module talks to the network.
"""

from __future__ import annotations

import logging
import threading
import weakref
from collections.abc import Callable, Iterator
from typing import Any

from core.interfaces import Health
from tts.sentence_splitter import split_sentences
from tts.stream_client import SpeechStream

logger = logging.getLogger(__name__)


class MockTTS:
    """Prints the reply and, when ``pyttsx3`` is installed, speaks it offline.

    It yields no PCM, so :class:`audio.speaker.AudioPlayer` never opens a sound
    device, but the mic mute flag is still set while speaking.
    """

    sample_rate: int = 24000

    def __init__(self, settings: Any = None, output: Callable[[str], None] = print) -> None:
        self.settings = settings
        self._output = output
        self._warned = False

    def speak(self, text: str) -> None:
        """Print ``text`` and say it with the Windows offline voice if available."""
        self._output(f"[voice] {text}")
        try:
            import pyttsx3
        except ImportError:
            if not self._warned:
                logger.info("pyttsx3 not installed; mock voice is text-only")
                self._warned = True
            return
        try:
            engine = pyttsx3.init()
            engine.say(text)
            engine.runAndWait()
            engine.stop()
        except Exception:  # noqa: BLE001 - offline voice is optional
            logger.warning("Offline voice failed", exc_info=True)

    def stream(self, text: str, language: str | None) -> Iterator[bytes]:
        """Same interface as ``SarvamTTS.stream``; speaks, then yields nothing."""
        self.speak(text)
        yield from ()

    def cancel(self) -> None:
        """Nothing to cancel (speech is synchronous)."""

    def health(self) -> Health:
        """Always healthy."""
        return Health("tts", True, "mock voice")


class SilentTTS:
    """Yields silence as long as the reply would take to speak.

    Args:
        sample_rate: PCM rate of the chunks (24000 like bulbul:v3).
        ms_per_char: speaking time per character (~60 ms is a natural pace).
        chunk_ms: length of each yielded chunk.
        latency_ms: delay before each sentence's first chunk (network + synthesis).
        realtime: produce chunks no faster than real time (else as fast as possible).

    Attributes:
        spoken: ``(text, language)`` of every request, in order.
    """

    def __init__(
        self,
        sample_rate: int = 24000,
        ms_per_char: float = 60.0,
        *,
        chunk_ms: int = 100,
        latency_ms: int = 0,
        realtime: bool = False,
    ) -> None:
        self.sample_rate = sample_rate
        self.ms_per_char = ms_per_char
        self.chunk_ms = chunk_ms
        self.latency_ms = latency_ms
        self.realtime = realtime
        self.spoken: list[tuple[str, str | None]] = []
        self._streams: weakref.WeakSet[SpeechStream] = weakref.WeakSet()
        self._lock = threading.Lock()

    def duration_ms(self, text: str) -> int:
        """How long ``text`` would take to say."""
        return int(len(text.strip()) * self.ms_per_char)

    def stream(self, text: str, language: str | None) -> SpeechStream:
        """Silent PCM chunks, sentence by sentence; cancel/close stop at once."""
        self.spoken.append((text, language))
        sentences = split_sentences(text)
        stream = SpeechStream(lambda job: self._produce(job, sentences))
        with self._lock:
            self._streams.add(stream)
        return stream

    def _produce(self, job: SpeechStream, sentences: list[str]) -> None:
        wait = threading.Event()  # interruptible sleep: cancel hook sets it
        job.add_cancel_hook(wait.set)
        chunk_bytes = self.sample_rate * self.chunk_ms // 1000 * 2
        for sentence in sentences:
            if wait.wait(self.latency_ms / 1000):
                return
            remaining = self.duration_ms(sentence) * self.sample_rate // 1000 * 2
            while remaining > 0:
                size = min(chunk_bytes, remaining)
                if not job.put(bytes(size)):
                    return
                remaining -= size
                if self.realtime and wait.wait(size / 2 / self.sample_rate):
                    return

    def cancel(self) -> None:
        """Cancel every stream in progress."""
        with self._lock:
            streams = list(self._streams)
        for stream in streams:
            stream.cancel()

    def health(self) -> Health:
        """Always healthy."""
        return Health("tts", True, "silent fake")

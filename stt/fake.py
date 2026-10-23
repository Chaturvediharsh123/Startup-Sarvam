"""Fakes for WS2: scripted transcripts instead of Sarvam speech-to-text.

* :class:`FakeSTT` implements ``core.interfaces.SpeechToText`` with the same
  constructor callbacks as :class:`stt.realtime_client.RealtimeSTT`. ``run_blocking``
  "hears" each scripted sentence: ``speech_start``, growing partials,
  ``speech_end``, then the final; afterwards it keeps reading (and discarding) mic
  audio until ``stop`` is set, like a live socket would.
* :class:`FakeBatchSTT` implements ``core.interfaces.BatchSpeechToText`` and returns
  scripted ``(text, language)`` results in order.

Nothing here touches the network.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence

from core.interfaces import AudioReader, Health

Transcript = tuple[str, str | None]


class FakeSTT:
    """Emits a scripted list of ``(text, language)`` finals, with partials before each.

    Args:
        script: sentences to "hear", in order.
        on_partial: called with the growing text (word by word).
        on_final: called with ``(text, language)`` for each sentence.
        on_event: receives ``connected``, ``speech_start`` and ``speech_end``.
        delay_s: pause between partials and between sentences.
        start_delay_s: pause after ``connected`` before the first sentence.
    """

    def __init__(
        self,
        script: Sequence[Transcript],
        on_partial: Callable[[str], None],
        on_final: Callable[[str, str | None], None],
        on_event: Callable[[str], None] | None = None,
        *,
        delay_s: float = 0.05,
        start_delay_s: float = 0.0,
    ) -> None:
        self.script = list(script)
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_event = on_event or (lambda _e: None)
        self.delay_s = delay_s
        self.start_delay_s = start_delay_s
        self.language_hint = ""
        self.connected = False
        self.audio_chunks = 0

    @classmethod
    def factory(
        cls, script: Sequence[Transcript], **kwargs: float
    ) -> Callable[..., FakeSTT]:
        """A ``(on_partial, on_final, on_event) -> FakeSTT`` builder for the orchestrator."""

        def build(
            on_partial: Callable[[str], None],
            on_final: Callable[[str, str | None], None],
            on_event: Callable[[str], None] | None = None,
        ) -> FakeSTT:
            return cls(script, on_partial, on_final, on_event, **kwargs)

        return build

    def run_blocking(self, read_audio: AudioReader, stop: threading.Event) -> None:
        """Play the script, then discard mic audio until ``stop`` is set."""
        self.connected = True
        self.on_event("connected")
        try:
            if stop.wait(self.start_delay_s):
                return
            for text, language in self.script:
                if not self._say(text, language, stop):
                    return
            while not stop.is_set():
                if read_audio(0.1):
                    self.audio_chunks += 1
        finally:
            self.connected = False

    def _say(self, text: str, language: str | None, stop: threading.Event) -> bool:
        self.on_event("speech_start")
        words = text.split()
        for count in range(1, len(words)):
            self.on_partial(" ".join(words[:count]))
            if stop.wait(self.delay_s):
                return False
        self.on_event("speech_end")
        self.on_final(text, language)
        return not stop.wait(self.delay_s)

    def health(self) -> Health:
        """Always healthy; says it is a fake."""
        return Health("stt", True, "fake stt" + (" live" if self.connected else ""))


class FakeBatchSTT:
    """Returns scripted transcripts for push-to-talk recordings.

    Args:
        results: ``(text, language)`` per call, in order.
        default: returned once ``results`` is used up.

    Attributes:
        calls: ``(byte_count, sample_rate)`` of every recording received.
    """

    def __init__(
        self, results: Sequence[Transcript] = (), default: Transcript = ("", None)
    ) -> None:
        self.results = list(results)
        self.default = default
        self.language_hint = ""
        self.calls: list[tuple[int, int]] = []

    def transcribe(self, pcm: bytes, sample_rate: int) -> tuple[str, str | None]:
        """Next scripted result (the audio itself is ignored)."""
        self.calls.append((len(pcm), sample_rate))
        return self.results.pop(0) if self.results else self.default

    def health(self) -> Health:
        """Always healthy."""
        return Health("stt_batch", True, "fake batch stt")

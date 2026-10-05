"""The real-time loop: mic -> STT -> brain -> safety -> TTS -> speaker.

Threads:

* **STT thread** (live mode): its own asyncio loop for the realtime WebSocket.
  Partial and final transcripts arrive through callbacks.
* **Worker thread**: handles one turn at a time (brain, actions, TTS playback),
  so the UI never freezes.
* **Push-to-talk threads**: record while the key is held, then transcribe.

Everything the UI shows goes through :attr:`Pipeline.events`, a thread-safe queue
of :class:`PipelineEvent` with kinds ``partial``, ``final``, ``reply``,
``action``, ``status``, ``timing``, ``error``, ``level`` and ``language``.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from assistant.brain import Brain
from assistant.config import Settings
from audio.mic import Microphone, MuteFlag, parse_device
from audio.player import AudioPlayer
from audio.wakeword import strip_wake_word
from language.detect import detect_language
from sarvam.client import SarvamClient
from sarvam.stt_realtime import RealtimeSTT
from sarvam.stt_rest import RestSTT
from sarvam.tts import SarvamTTS

logger = logging.getLogger(__name__)

_STOP = object()


@dataclass(frozen=True)
class PipelineEvent:
    """One message for the UI or terminal."""

    kind: str
    data: Any = None


class SpeechToText(Protocol):
    """What the pipeline needs from a live STT client."""

    def run_blocking(
        self, read_audio: Callable[[float], bytes | None], stop: threading.Event
    ) -> None:
        """Stream audio until ``stop`` is set."""
        ...


SttFactory = Callable[
    [Callable[[str], None], Callable[[str, str | None], None], Callable[[str], None]],
    SpeechToText,
]


@dataclass
class _Turn:
    """One queued user utterance waiting for the worker."""

    text: str
    language: str | None
    stt_ms: int | None


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


class Pipeline:
    """Connects every part of the voice assistant.

    Args:
        settings: app settings (wake word, confirm timeout, barge-in).
        brain: turns text into actions and replies.
        tts: produces PCM chunks for a reply.
        player: plays PCM chunks.
        mic: microphone (shared by live mode and push-to-talk).
        stt_factory: builds a live STT client from (on_partial, on_final, on_event).
        rest_stt: push-to-talk transcription.
    """

    def __init__(
        self,
        settings: Settings,
        brain: Brain,
        tts: Any,
        player: AudioPlayer,
        mic: Microphone,
        stt_factory: SttFactory,
        rest_stt: Any = None,
    ) -> None:
        self.settings = settings
        self.brain = brain
        self.tts = tts
        self.player = player
        self.mic = mic
        self.rest_stt = rest_stt
        self.events: queue.Queue[PipelineEvent] = queue.Queue()
        self._stt_factory = stt_factory
        self._turns: queue.Queue[Any] = queue.Queue()
        self._confirm_answers: queue.Queue[str] = queue.Queue()
        self._confirm_pending = threading.Event()
        self._busy = threading.Event()
        self._live_stop = threading.Event()
        self._live_thread: threading.Thread | None = None
        self._ptt_stop = threading.Event()
        self._ptt_thread: threading.Thread | None = None
        self._ptt_audio: list[bytes] = []
        self._speech_end_at: float | None = None
        self._turn_language: str | None = None
        self._worker = threading.Thread(target=self._work, name="brain-worker", daemon=True)
        self._worker.start()

    # -- events --------------------------------------------------------------
    def emit(self, kind: str, data: Any = None) -> None:
        """Queue an event for the UI."""
        self.events.put(PipelineEvent(kind, data))

    # -- live mode -------------------------------------------------------------
    @property
    def live(self) -> bool:
        """True while live listening is on."""
        return self._live_thread is not None and self._live_thread.is_alive()

    def start_live(self) -> bool:
        """Open the mic and the realtime STT connection. Returns False on failure."""
        if self.live:
            return True
        try:
            self.mic.start()
        except RuntimeError as exc:
            self.emit("error", str(exc))
            return False
        self._live_stop.clear()
        stt = self._stt_factory(self._on_partial, self._on_final, self._on_stt_event)
        self._live_thread = threading.Thread(
            target=self._run_stt, args=(stt,), name="stt-realtime", daemon=True
        )
        self._live_thread.start()
        self.emit("status", "listening")
        return True

    def _run_stt(self, stt: SpeechToText) -> None:
        try:
            stt.run_blocking(self.mic.read, self._live_stop)
        except Exception as exc:  # noqa: BLE001 - report and end live mode cleanly
            logger.exception("Live STT crashed")
            self.emit("error", f"Live listening stopped: {exc}")
        finally:
            # Only touch the mic if live mode was not restarted meanwhile.
            if self._live_thread in (None, threading.current_thread()):
                self.mic.stop()
            self.emit("status", "stopped")

    def stop_live(self) -> None:
        """Close the WebSocket (no more Sarvam credits) and the mic."""
        self._live_stop.set()
        thread, self._live_thread = self._live_thread, None
        if thread is not None:
            thread.join(timeout=5)
            if thread.is_alive():
                logger.warning("STT thread did not stop within 5 s")
        self.mic.stop()

    # -- STT callbacks (run on the STT thread) -----------------------------------
    def _on_partial(self, text: str) -> None:
        self.emit("partial", text)

    def _on_stt_event(self, event: str) -> None:
        if event == "speech_end":
            self._speech_end_at = time.perf_counter()
        elif event == "speech_start" and self.settings.barge_in and self.player.is_speaking:
            self.player.stop()
        elif event.startswith("error:"):
            self.emit("error", event[6:])
        elif event in ("connected", "disconnected"):
            self.emit("status", event)

    def _on_final(self, text: str, language: str | None) -> None:
        """A finished utterance: a confirmation answer, a new turn, or ignored."""
        self.emit("final", text)
        if language:
            self.emit("language", language)
        stt_ms = _ms(self._speech_end_at) if self._speech_end_at else None
        self._speech_end_at = None
        if self._confirm_pending.is_set():
            self._confirm_answers.put(text)
            return
        if self._busy.is_set():
            self.emit("status", "busy: ignored speech while answering")
            return
        command = strip_wake_word(text, self.settings.wake_word)
        if not command:
            self.emit("status", "waiting for wake word")
            return
        self.submit(command, language, stt_ms)

    def submit(self, text: str, language: str | None = None, stt_ms: int | None = None) -> None:
        """Queue a turn (typed text, push-to-talk or live final)."""
        self._busy.set()
        self._turns.put(_Turn(text, language, stt_ms))

    def submit_typed(self, text: str) -> None:
        """Typed input from the window: a yes/no answer if one is pending, else a new turn."""
        text = text.strip()
        if not text:
            return
        self.emit("final", text)
        if self._confirm_pending.is_set():
            self._confirm_answers.put(text)
        elif self._busy.is_set():
            self.emit("status", "busy: wait for the current answer")
        else:
            self.submit(text)

    # -- push-to-talk ------------------------------------------------------------
    def ptt_press(self) -> None:
        """Start recording (key or button held down). Ignored during live mode."""
        if self.live or self._ptt_thread is not None or self.rest_stt is None:
            return
        try:
            self.mic.start()
        except RuntimeError as exc:
            self.emit("error", str(exc))
            return
        self.mic.drain()
        self._ptt_audio = []
        self._ptt_stop.clear()
        self._ptt_thread = threading.Thread(target=self._record, name="ptt-record", daemon=True)
        self._ptt_thread.start()
        self.emit("status", "recording")

    def _record(self) -> None:
        while not self._ptt_stop.is_set():
            chunk = self.mic.read(0.1)
            if chunk:
                self._ptt_audio.append(chunk)

    def ptt_release(self) -> None:
        """Stop recording and transcribe in the background."""
        thread, self._ptt_thread = self._ptt_thread, None
        if thread is None:
            return
        self._ptt_stop.set()
        thread.join(timeout=2)
        self.mic.stop()
        audio = b"".join(self._ptt_audio)
        threading.Thread(target=self._transcribe, args=(audio,), daemon=True).start()

    def _transcribe(self, audio: bytes) -> None:
        self.emit("status", "transcribing")
        start = time.perf_counter()
        try:
            text, language = self.rest_stt.transcribe(audio, self.settings.sample_rate)
        except Exception as exc:  # noqa: BLE001 - show the problem, keep running
            logger.exception("Push-to-talk transcription failed")
            self.emit("error", f"Could not transcribe: {exc}")
            return
        if not text:
            self.emit("status", "heard nothing")
            if self._confirm_pending.is_set():
                self._confirm_answers.put("")
            return
        self._speech_end_at = start
        self._on_final(text, language)

    # -- speaking ---------------------------------------------------------------
    def stop_speaking(self) -> None:
        """Interrupt the current reply (F9 / Stop button)."""
        self.player.stop()

    def speak(self, text: str, language: str | None) -> int | None:
        """Speak ``text``; returns milliseconds until the first audio, or ``None``."""
        start = time.perf_counter()
        first: list[int] = []
        try:
            self.player.play(
                self.tts.stream(text, language), on_first_audio=lambda: first.append(_ms(start))
            )
        except Exception as exc:  # noqa: BLE001 - a voice failure must not end the loop
            logger.exception("Speaking failed")
            self.emit("error", f"Could not speak the reply: {exc}")
        finally:
            self.mic.drain()  # drop anything recorded while speaking
        return first[0] if first else None

    def ask_user(self, question: str) -> str:
        """Speak a yes/no question and wait for one answer (timeout counts as no)."""
        self.emit("reply", question)
        while not self._confirm_answers.empty():
            self._confirm_answers.get_nowait()
        self.speak(question, self._turn_language)
        self._confirm_pending.set()
        self.emit("status", "waiting for yes/no")
        try:
            return self._confirm_answers.get(timeout=self.settings.confirm_timeout_s)
        except queue.Empty:
            self.emit("status", "no answer: treated as no")
            return ""
        finally:
            self._confirm_pending.clear()

    # -- worker -----------------------------------------------------------------
    def _work(self) -> None:
        while True:
            item = self._turns.get()
            if item is _STOP:
                return
            try:
                self._handle_turn(item)
            except Exception as exc:  # noqa: BLE001 - one bad turn must not kill the loop
                logger.exception("Turn failed")
                self.emit("error", f"Something went wrong: {exc}")
            finally:
                if self._turns.empty():
                    self._busy.clear()

    def _handle_turn(self, turn: _Turn) -> None:
        self._turn_language = detect_language(
            turn.text, turn.language, self.settings.default_language
        ).code
        self.emit("status", "thinking")
        reply = self.brain.handle(turn.text, turn.language, ask_user=self.ask_user)
        for outcome in reply.actions:
            self.emit("action", outcome)
        self.emit("reply", reply.text)
        self.emit("status", "speaking")
        first_audio = self.speak(reply.text, reply.language)
        timings = dict(reply.timings)
        timings["stt_ms"] = turn.stt_ms or 0
        timings["tts_first_ms"] = first_audio or 0
        timings["total_ms"] = timings["stt_ms"] + reply.timings.get("total_ms", 0) + (
            first_audio or 0
        )
        self.emit("timing", timings)
        self.emit("status", "listening" if self.live else "ready")

    # -- lifecycle ----------------------------------------------------------------
    def shutdown(self) -> None:
        """Stop listening, speaking and the worker thread."""
        self.stop_live()
        self.ptt_release()
        self.player.stop()
        self._confirm_answers.put("")  # unblock a pending confirmation
        self._turns.put(_STOP)
        self._worker.join(timeout=10)
        if self._worker.is_alive():
            logger.warning("Worker thread still busy at shutdown")


class _NoLiveSTT:
    """Mock mode has no speech recognition; live mode reports that and stops."""

    def __init__(self, on_event: Callable[[str], None]) -> None:
        self._on_event = on_event

    def run_blocking(
        self, read_audio: Callable[[float], bytes | None], stop: threading.Event
    ) -> None:
        """Report that live listening is unavailable and return."""
        self._on_event("error:Live listening needs a Sarvam API key (mock mode)")


def create_mock_pipeline(settings: Settings, brain: Brain) -> Pipeline:
    """Pipeline for ``--mock``: typed input, MockTTS voice, no Sarvam calls."""
    from sarvam.mock import MockTTS

    mute = MuteFlag()
    mic = Microphone(
        settings.sample_rate, settings.chunk_ms, mute, parse_device(settings.mic_device)
    )
    player = AudioPlayer(settings.tts_sample_rate, mute)
    return Pipeline(
        settings,
        brain,
        MockTTS(settings),
        player,
        mic,
        lambda _partial, _final, on_event: _NoLiveSTT(on_event),
        rest_stt=None,
    )


def create_pipeline(settings: Settings, client: SarvamClient, brain: Brain) -> Pipeline:
    """Build a pipeline with the real mic, speaker and Sarvam clients."""
    mute = MuteFlag()
    mic = Microphone(
        settings.sample_rate, settings.chunk_ms, mute, parse_device(settings.mic_device)
    )
    # With barge-in on, the mic stays open while speaking (use headphones).
    player = AudioPlayer(settings.tts_sample_rate, None if settings.barge_in else mute)

    def stt_factory(
        on_partial: Callable[[str], None],
        on_final: Callable[[str, str | None], None],
        on_event: Callable[[str], None],
    ) -> RealtimeSTT:
        """Build a realtime STT client wired to the pipeline callbacks."""
        return RealtimeSTT(client, settings, on_partial, on_final, on_event)

    pipeline = Pipeline(
        settings,
        brain,
        SarvamTTS(client, settings),
        player,
        mic,
        stt_factory,
        RestSTT(client, settings),
    )
    mic.on_level = lambda level: pipeline.emit("level", level)
    return pipeline

"""The orchestrator: the only module that knows the order of a turn.

One turn (PDF "Runtime flow of one turn")::

    FinalTranscript -> wake word / stop word -> Brain.decide -> Guard.check
      -> (confirm aloud -> yes?) -> ActionRunner.run -> Brain.summarise -> TTS -> speaker

Every step publishes a typed event from :mod:`core.events` on the bus, carries the
turn id, and moves the :class:`~core.state.StateMachine` through the PDF states.

Threads:

* **STT thread** (live mode): the realtime client's own asyncio loop. Its
  callbacks only publish events and queue turns.
* **Worker thread**: one turn at a time (brain, safety, action, TTS playback), so
  the UI never freezes.
* **Push-to-talk threads**: record while the key is held, then transcribe.
* **Health thread**: polls each module's ``health()`` every few seconds.

Interrupt (Stop button, F9, a spoken "ruko", the emergency-stop corner) cancels the
current turn's :class:`~core.interfaces.CancelToken`, stops playback, answers a
pending confirmation with "no" and returns to Listening.

The orchestrator imports only ``core`` and the shared ``language`` utilities. The
concrete modules are built and passed in by ``main.py``.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol

from core.bus import EventBus
from core.config import Settings
from core.events import (
    ActionResult,
    BrainResult,
    ErrorRaised,
    FinalTranscript,
    HealthChanged,
    Interrupt,
    LanguageDetected,
    Metrics,
    MicLevel,
    PartialTranscript,
    SafetyDecision,
    SpeakFinished,
    SpeakRequest,
    SpeakStarted,
    Status,
    ToolCall,
    TurnState,
    Verdict,
    new_turn_id,
)
from core.interfaces import (
    ActionRunner,
    BatchSpeechToText,
    Brain,
    CancelToken,
    Guard,
    Health,
    SpeechToText,
    TextToSpeech,
)
from core.logging import current_turn
from core.state import StateMachine
from language.detect import detect_language
from language.phrases import is_stop_command, phrase

logger = logging.getLogger(__name__)

_STOP = object()
# A fast action finishing within this time is reported in one sentence; a slower
# one first gets the brain's "doing it" sentence while it runs (PDF latency trick).
EARLY_REPLY_AFTER_S = 0.4
HEALTH_POLL_S = 5.0


class Microphone(Protocol):
    """What the orchestrator needs from the mic (``audio.mic.Microphone`` or a fake)."""

    def start(self) -> None:
        """Open the input stream (raises RuntimeError on device problems)."""

    def stop(self) -> None:
        """Close the input stream."""

    def read(self, timeout: float) -> bytes | None:
        """Next chunk or None."""
        ...

    def drain(self) -> None:
        """Drop queued chunks."""


class Speaker(Protocol):
    """What the orchestrator needs from the speaker (``audio.speaker.AudioPlayer``)."""

    def play(
        self, chunks: Iterable[bytes], on_first_audio: Callable[[], None] | None = None
    ) -> None:
        """Play PCM chunks until done or :meth:`stop` (blocking)."""

    def stop(self) -> None:
        """Stop playback within 100 ms."""

    @property
    def is_speaking(self) -> bool:
        """True while audio is playing."""
        ...


SttFactory = Callable[
    [Callable[[str], None], Callable[[str, str | None], None], Callable[[str], None]],
    SpeechToText,
]
WakeFilter = Callable[[str, str], str | None]
"""``(text, wake_word) -> command`` with the wake word removed, or ``None``/``""`` to ignore."""


@dataclass
class _Turn:
    """One queued user utterance waiting for the worker."""

    text: str
    language: str | None
    speech_end: float  # perf_counter when the user stopped speaking (or typed)
    stt_ms: int = 0
    turn_id: str = ""


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _no_wake_filter(text: str, wake_word: str) -> str:
    return text.strip()


class Orchestrator:
    """Runs turns through the PDF state machine and publishes every step on the bus.

    Args:
        settings: thresholds and timeouts.
        bus: the app's event bus.
        brain, guard, runner, tts: the modules, through their ``core.interfaces``.
        player, mic: speaker and microphone.
        stt_factory: builds a live STT client from ``(on_partial, on_final, on_event)``.
        batch_stt: one-shot transcription for push-to-talk and fallback.
        wake_filter: strips the wake word (``stt.wake_word.strip_wake_word``).
        modules: objects with ``health()`` shown as status dots, keyed by module name.
        before_turn: called on the worker thread when a new user turn starts (``main``
            uses it to lift an emergency stop once the user gives a fresh command).
    """

    def __init__(
        self,
        settings: Settings,
        bus: EventBus,
        brain: Brain,
        guard: Guard,
        runner: ActionRunner,
        tts: TextToSpeech,
        player: Speaker,
        mic: Microphone,
        stt_factory: SttFactory,
        batch_stt: BatchSpeechToText | None = None,
        wake_filter: WakeFilter | None = None,
        modules: dict[str, Any] | None = None,
        before_turn: Callable[[], None] | None = None,
    ) -> None:
        self.settings = settings
        self.bus = bus
        self.brain = brain
        self.guard = guard
        self.runner = runner
        self.tts = tts
        self.player = player
        self.mic = mic
        self.batch_stt = batch_stt
        self._stt_factory = stt_factory
        self._wake_filter = wake_filter or _no_wake_filter
        self.wake_word = settings.wake_word
        self.language_hint = settings.language_hint or None
        self.modules = dict(modules or {})
        self._before_turn = before_turn
        self.state = StateMachine(bus.publish)

        self._turns: queue.Queue[Any] = queue.Queue()
        self._confirm_answers: queue.Queue[str] = queue.Queue()
        self._confirm_pending = threading.Event()
        self._busy = threading.Event()
        self._speak_lock = threading.Lock()
        self._token = CancelToken()
        self._turn_id = ""
        self._first_sound_at: float | None = None

        self._live_stop = threading.Event()
        self._live_thread: threading.Thread | None = None
        self._stt_down = False
        self._ptt_stop = threading.Event()
        self._ptt_thread: threading.Thread | None = None
        self._ptt_audio: list[bytes] = []
        self._speech_end_at: float | None = None
        self._last_health: dict[str, tuple[bool, str]] = {}
        self._closed = threading.Event()

        self._worker = threading.Thread(target=self._work, name="turn-worker", daemon=True)
        self._worker.start()
        self._health_thread = threading.Thread(
            target=self._poll_health, name="health", daemon=True
        )
        self._health_thread.start()

    # -- small helpers --------------------------------------------------------------
    def publish(self, event: Any) -> None:
        """Publish ``event`` stamped with the current turn id (if it has none)."""
        if not event.turn_id:
            event.turn_id = self._turn_id
        self.bus.publish(event)

    def status(self, text: str) -> None:
        """Publish a short status line for the UI."""
        self.publish(Status(text))

    def error(self, message: str, module: str = "") -> None:
        """Publish a problem for the UI. Errors never crash the loop."""
        logger.warning("%s%s", f"{module}: " if module else "", message)
        self.publish(ErrorRaised(message, module))

    @property
    def busy(self) -> bool:
        """True while a turn is queued or running."""
        return self._busy.is_set()

    @property
    def awaiting_answer(self) -> bool:
        """True while a spoken yes/no question waits for the user's answer."""
        return self._confirm_pending.is_set()

    # -- live mode ----------------------------------------------------------------
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
        except (RuntimeError, OSError) as exc:
            self.error(str(exc), "audio")
            self._set_health("audio", False, str(exc))
            return False
        self._live_stop.clear()
        stt = self._stt_factory(self._on_partial, self._on_final, self._on_stt_event)
        if hasattr(stt, "language_hint"):
            stt.language_hint = self.language_hint or ""
        self._live_thread = threading.Thread(
            target=self._run_stt, args=(stt,), name="stt-realtime", daemon=True
        )
        self._live_thread.start()
        self.state.reset(live=True)
        self.status("listening")
        return True

    def _run_stt(self, stt: SpeechToText) -> None:
        try:
            stt.run_blocking(self.mic.read, self._live_stop)
        except Exception as exc:  # noqa: BLE001 - report and end live mode cleanly
            logger.exception("Live STT crashed")
            self.error(f"Live listening stopped: {exc}", "stt")
        finally:
            if self._live_thread in (None, threading.current_thread()):
                self.mic.stop()
                if not self._busy.is_set():
                    self.state.reset(live=False)
            self.status("stopped")

    def stop_live(self) -> None:
        """Close the WebSocket (no more Sarvam credits) and the mic."""
        self._live_stop.set()
        thread, self._live_thread = self._live_thread, None
        if thread is not None:
            thread.join(timeout=5)
            if thread.is_alive():
                logger.warning("STT thread did not stop within 5 s")
        self.mic.stop()
        if not self._busy.is_set():
            self.state.reset(live=False)

    def set_language_hint(self, code: str | None) -> None:
        """Language hint for STT and the brain (``None``/empty = auto-detect)."""
        self.language_hint = code or None
        if self.batch_stt is not None and hasattr(self.batch_stt, "language_hint"):
            self.batch_stt.language_hint = self.language_hint or ""
        self.status(f"language: {self.language_hint or 'auto'}")
        if self.live:  # the realtime socket takes the hint on connect
            self.stop_live()
            self.start_live()

    def set_wake_word(self, word: str) -> None:
        """Change the wake word (empty = off)."""
        self.wake_word = word.strip()

    # -- STT callbacks (run on the STT thread) ---------------------------------------
    def _on_partial(self, text: str) -> None:
        self.publish(PartialTranscript(text))

    def _on_stt_event(self, event: str) -> None:
        if event in ("speech_end", "local_end_of_speech"):
            self._speech_end_at = self._speech_end_at or time.perf_counter()
        elif event == "speech_start":
            if self.settings.barge_in and self.player.is_speaking:
                self.interrupt("barge-in")
        elif event == "connected":
            self._stt_down = False
            self._set_health("stt", True, "connected")
            self.status("listening")
        elif event == "disconnected":
            self._set_health("stt", False, "reconnecting")
        elif event == "fallback":
            self._stt_down = True
            self._set_health("stt", False, "offline: use hold-to-talk")
            self.error("Live listening is offline. Hold the talk button (F8) instead.", "stt")
        elif event.startswith("error:"):
            self.error(event[6:], "stt")

    def on_mic_level(self, level: float) -> None:
        """Mic loudness callback for the UI level meter."""
        self.bus.publish(MicLevel(level))

    def _on_final(self, text: str, language: str | None) -> None:
        """A finished utterance: a stop word, a yes/no answer, a new turn, or ignored."""
        speech_end = self._speech_end_at or time.perf_counter()
        self._speech_end_at = None
        if self.settings.stop_words_enabled and is_stop_command(text):
            self.publish(FinalTranscript(text, language))
            if self._busy.is_set() or self.player.is_speaking or self._confirm_pending.is_set():
                self.interrupt("voice")
            return
        if self._confirm_pending.is_set():
            self.publish(FinalTranscript(text, language))
            self._confirm_answers.put(text)
            return
        if self._busy.is_set():
            self.publish(FinalTranscript(text, language))
            self.status("busy: ignored speech while answering")
            return
        command = self._wake_filter(text, self.wake_word) if self.wake_word else text.strip()
        if not command:
            self.publish(FinalTranscript(text, language))
            self.status("waiting for wake word")
            return
        turn_id = self.submit(command, language, speech_end=speech_end, stt_ms=_ms(speech_end))
        self.publish(FinalTranscript(text, language, turn_id=turn_id))
        if language:
            self.publish(LanguageDetected(language, turn_id=turn_id))

    def submit(
        self,
        text: str,
        language: str | None = None,
        speech_end: float | None = None,
        stt_ms: int = 0,
    ) -> str:
        """Queue a turn (typed text, push-to-talk or live final). Returns its turn id."""
        turn = _Turn(
            text,
            language or self.language_hint,
            speech_end or time.perf_counter(),
            stt_ms,
            new_turn_id(),
        )
        self._busy.set()
        self._turns.put(turn)
        return turn.turn_id

    def submit_typed(self, text: str) -> None:
        """Typed input: a yes/no answer if one is pending, a stop word, else a new turn."""
        text = text.strip()
        if not text:
            return
        if self._confirm_pending.is_set():
            self.publish(FinalTranscript(text, None))
            self._confirm_answers.put(text)
        elif self.settings.stop_words_enabled and is_stop_command(text) and self._busy.is_set():
            self.publish(FinalTranscript(text, None))
            self.interrupt("voice")
        elif self._busy.is_set():
            self.status("busy: wait for the current answer")
        else:
            turn_id = self.submit(text)
            self.publish(FinalTranscript(text, None, turn_id=turn_id))

    # -- push-to-talk ---------------------------------------------------------------
    def ptt_press(self) -> None:
        """Start recording (key or button held). In live mode only while STT is offline."""
        if (self.live and not self._stt_down) or self._ptt_thread is not None:
            return
        if self.batch_stt is None:
            self.error("Push-to-talk needs speech recognition (not available here)", "stt")
            return
        if not self.live:
            try:
                self.mic.start()
            except (RuntimeError, OSError) as exc:
                self.error(str(exc), "audio")
                return
        self.mic.drain()
        self._ptt_audio = []
        self._ptt_stop.clear()
        self._ptt_thread = threading.Thread(target=self._record, name="ptt-record", daemon=True)
        self._ptt_thread.start()
        self.status("recording")

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
        if not self.live:
            self.mic.stop()
        audio = b"".join(self._ptt_audio)
        threading.Thread(target=self._transcribe, args=(audio,), daemon=True).start()

    def _transcribe(self, audio: bytes) -> None:
        self.status("transcribing")
        start = time.perf_counter()
        try:
            text, language = self.batch_stt.transcribe(  # type: ignore[union-attr]
                audio, self.settings.sample_rate
            )
        except Exception as exc:  # noqa: BLE001 - show the problem, keep running
            logger.exception("Push-to-talk transcription failed")
            self.error(f"Could not transcribe: {exc}", "stt")
            if self._confirm_pending.is_set():
                self._confirm_answers.put("")
            return
        if not text:
            self.status("heard nothing")
            if self._confirm_pending.is_set():
                self._confirm_answers.put("")
            return
        self._speech_end_at = start
        self._on_final(text, language)

    # -- interrupt ------------------------------------------------------------------
    def interrupt(self, reason: str = "user") -> None:
        """Stop everything for the current turn and go back to Listening (PDF)."""
        logger.info("Interrupt (%s)", reason)
        self._token.cancel(reason)
        self.player.stop()
        cancel_tts = getattr(self.tts, "cancel", None)
        if callable(cancel_tts):
            cancel_tts()
        if self._confirm_pending.is_set():
            self._confirm_answers.put("")
        self.publish(Interrupt(reason))
        if not self._busy.is_set():
            self.state.reset(self.live, self._turn_id)
        self.status("stopped")

    def stop_speaking(self) -> None:
        """Older name for :meth:`interrupt` (F9 / Stop button)."""
        self.interrupt("user")

    # -- speaking -------------------------------------------------------------------
    def speak(self, text: str, language: str | None, mark_first_sound: bool = True
              ) -> int | None:
        """Speak ``text`` (blocking). Returns ms until the first audio, or ``None``."""
        if not text or self._token.cancelled:
            return None
        with self._speak_lock:
            if self._token.cancelled:
                return None
            start = time.perf_counter()
            first: list[int] = []

            def on_first_audio() -> None:
                first.append(_ms(start))
                if mark_first_sound and self._first_sound_at is None:
                    self._first_sound_at = time.perf_counter()
                self.publish(SpeakStarted())

            self.publish(SpeakRequest(text, language))
            source: Any = None
            chunks: Iterator[bytes] | None = None
            interrupted = False
            try:
                source = self.tts.stream(text, language)
                chunks = iter(source)
                self.player.play(chunks, on_first_audio=on_first_audio)
                interrupted = self._token.cancelled
            except Exception as exc:  # noqa: BLE001 - a voice failure must not end the loop
                logger.exception("Speaking failed")
                self.error(f"Could not speak the reply: {exc}", "tts")
                self._set_health("tts", False, str(exc))
            finally:
                for stream in (chunks, source):  # stop TTS prefetch threads promptly
                    close = getattr(stream, "close", None)
                    if callable(close):
                        close()
                self.mic.drain()  # drop anything recorded while speaking
                if first:
                    self.publish(SpeakFinished(interrupted=interrupted))
            return first[0] if first else None

    def ask_user(self, question: str, language: str | None) -> str:
        """Speak a yes/no question and wait for one answer. Timeout or interrupt = "" (no)."""
        while not self._confirm_answers.empty():
            self._confirm_answers.get_nowait()
        self.speak(question, language)
        self._confirm_pending.set()
        self.status("waiting for yes/no")
        deadline = time.monotonic() + self.settings.confirm_timeout_s
        try:
            while time.monotonic() < deadline and not self._token.cancelled:
                try:
                    return self._confirm_answers.get(timeout=0.1)
                except queue.Empty:
                    continue
            self.status("no answer: treated as no")
            return ""
        finally:
            self._confirm_pending.clear()

    # -- worker ---------------------------------------------------------------------
    def _work(self) -> None:
        while True:
            item = self._turns.get()
            if item is _STOP:
                return
            try:
                self._handle_turn(item)
            except Exception as exc:  # noqa: BLE001 - one bad turn must not kill the loop
                logger.exception("Turn failed")
                self.error(f"Something went wrong: {exc}")
                self._speak_apology(item)
            finally:
                if self._turns.empty():
                    self._busy.clear()
                self.state.reset(self.live, self._turn_id)
                current_turn.set("-")

    def _speak_apology(self, turn: _Turn) -> None:
        """Errors in any module turn into a short spoken apology, never a crash (PDF)."""
        try:
            info = detect_language(turn.text, turn.language, self.settings.default_language)
            self._token = CancelToken()
            self.speak(phrase("sorry_repeat", info.code, romanised=info.romanised), info.code)
        except Exception:  # noqa: BLE001 - last resort, never raise from the worker
            logger.exception("Could not speak the apology")

    def _handle_turn(self, turn: _Turn) -> None:
        self._turn_id = turn.turn_id
        current_turn.set(turn.turn_id)
        self._token = token = CancelToken()
        self._first_sound_at = None
        if self._before_turn is not None:
            self._before_turn()
        watchdog = threading.Timer(self.settings.max_turn_s, token.cancel, args=("turn timeout",))
        watchdog.daemon = True
        watchdog.start()
        try:
            self._run_turn(turn, token)
        finally:
            watchdog.cancel()

    def _run_turn(self, turn: _Turn, token: CancelToken) -> None:
        info = detect_language(turn.text, turn.language, self.settings.default_language)
        self.state.to(TurnState.THINKING, turn.turn_id)
        self.status("thinking")

        filler = threading.Timer(
            self.settings.filler_after_ms / 1000,
            lambda: self.speak(
                phrase("filler", info.code, romanised=info.romanised), info.code,
                mark_first_sound=False,
            ),
        )
        filler.daemon = True
        if self.settings.filler_after_ms > 0:
            filler.start()
        try:
            result = self.brain.decide(turn.text, turn.language, token)
        finally:
            filler.cancel()
        result.turn_id = turn.turn_id
        self.publish(result)
        self.publish(LanguageDetected(result.language))
        if token.cancelled:
            return

        first_audio: int | None
        action_ms = 0
        if result.tool_call is None:
            self.state.to(TurnState.SPEAKING, turn.turn_id)
            spoken = result.reply
            first_audio = self.speak(result.reply, result.language)
        else:
            outcome, first_audio, spoken = self._act(result, token, turn)
            action_ms = outcome.duration_ms

        self._record(turn, result, spoken)
        sound_at = self._first_sound_at or time.perf_counter()
        self.publish(
            Metrics(
                stt_ms=turn.stt_ms,
                llm_ms=result.llm_ms,
                action_ms=action_ms,
                tts_first_ms=first_audio or 0,
                total_ms=max(0, int((sound_at - turn.speech_end) * 1000)),
            )
        )

    def _act(
        self, result: BrainResult, token: CancelToken, turn: _Turn
    ) -> tuple[ActionResult, int | None, str]:
        """Checking -> (Awaiting yes) -> Acting -> Speaking for the one tool call."""
        call = result.tool_call
        assert call is not None
        lang, roman = result.language, result.romanised
        self.state.to(TurnState.CHECKING, turn.turn_id)
        decision: SafetyDecision = self.guard.check(call, lang, roman)
        decision.turn_id = turn.turn_id
        self.publish(decision)

        if decision.verdict is Verdict.DENY:
            rate_limited = "rate" in decision.reason
            status = "blocked" if rate_limited or "allow-list" in decision.reason else "denied"
            outcome = ActionResult(
                call.name, dict(call.args), status, decision.reason,
                extra={"rate_limited": rate_limited}, turn_id=turn.turn_id,
            )
            return self._finish(result, outcome, token, turn, early_spoken=False)

        if decision.verdict is Verdict.CONFIRM:
            self.state.to(TurnState.AWAITING_YES, turn.turn_id)
            answer = self.ask_user(decision.question, lang)
            if token.cancelled or not self.guard.is_yes(answer):
                outcome = ActionResult(
                    call.name, dict(call.args), "denied", "user did not confirm",
                    turn_id=turn.turn_id,
                )
                self.publish(outcome)
                self._log_action(outcome)
                if token.cancelled:
                    return outcome, None, ""
                self.state.to(TurnState.SPEAKING, turn.turn_id)
                text = phrase("cancelled", lang, romanised=roman)
                return outcome, self.speak(text, lang), text

        if token.cancelled:
            outcome = ActionResult(
                call.name, dict(call.args), "cancelled", "interrupted", turn_id=turn.turn_id
            )
            self.publish(outcome)
            return outcome, None, ""

        self.state.to(TurnState.ACTING, turn.turn_id)
        approved = ToolCall(call.name, decision.clean_args or dict(call.args), call.call_id)
        box: dict[str, ActionResult] = {}

        def run() -> None:
            box["result"] = self.runner.run(approved, self.settings.action_timeout_s)

        worker = threading.Thread(target=run, name="action", daemon=True)
        worker.start()
        worker.join(EARLY_REPLY_AFTER_S)
        early_spoken = False
        first_audio: int | None = None
        if worker.is_alive() and result.reply and decision.verdict is Verdict.ALLOW:
            # Slow action: say the brain's "doing it" sentence while it runs (PDF trick).
            self.state.to(TurnState.SPEAKING, turn.turn_id)
            first_audio = self.speak(result.reply, lang)
            early_spoken = first_audio is not None
        worker.join(self.settings.action_timeout_s + 1)
        outcome = box.get("result") or ActionResult(
            call.name, dict(approved.args), "timeout", "action did not finish"
        )
        outcome.turn_id = turn.turn_id
        outcome, later_audio, text = self._finish(result, outcome, token, turn, early_spoken)
        return outcome, first_audio if first_audio is not None else later_audio, text

    def _finish(
        self,
        result: BrainResult,
        outcome: ActionResult,
        token: CancelToken,
        turn: _Turn,
        early_spoken: bool,
    ) -> tuple[ActionResult, int | None, str]:
        """Publish the outcome and speak the result sentence."""
        self.publish(outcome)
        self._log_action(outcome)
        if token.cancelled:
            return outcome, None, ""
        if early_spoken and outcome.ok and not outcome.informational:
            return outcome, None, result.reply  # "Chrome khol raha hoon" was enough
        text = self.brain.summarise(result, outcome)
        if self.state.state is not TurnState.SPEAKING:
            self.state.to(TurnState.SPEAKING, turn.turn_id)
        return outcome, self.speak(text, result.language), text

    def _log_action(self, outcome: ActionResult) -> None:
        record = getattr(self.brain, "record_action", None)
        if callable(record):
            try:
                record(outcome)
            except Exception:  # noqa: BLE001 - memory problems must not end the turn
                logger.exception("Could not record the action in memory")

    def _record(self, turn: _Turn, result: BrainResult, spoken: str) -> None:
        try:
            self.brain.record_turn(turn.text, spoken or result.reply, result.language)
        except Exception:  # noqa: BLE001 - memory problems must not end the turn
            logger.exception("Could not record the turn in memory")

    # -- health ---------------------------------------------------------------------
    def health(self) -> list[Health]:
        """Health of every registered module (for the UI status bar)."""
        report: list[Health] = []
        for name, module in self.modules.items():
            check = getattr(module, "health", None)
            if not callable(check):
                continue
            try:
                h = check()
                report.append(Health(name, bool(h.ok), str(h.detail)))
            except Exception as exc:  # noqa: BLE001 - a broken health check is unhealthy
                report.append(Health(name, False, f"health check failed: {exc}"))
        return report

    def _set_health(self, module: str, ok: bool, detail: str = "") -> None:
        if self._last_health.get(module) != (ok, detail):
            self._last_health[module] = (ok, detail)
            self.bus.publish(HealthChanged(module, ok, detail))

    def _poll_health(self) -> None:
        while not self._closed.is_set():
            for h in self.health():
                if not (h.module == "stt" and self._stt_down):
                    self._set_health(h.module, h.ok, h.detail)
            self._closed.wait(HEALTH_POLL_S)

    # -- lifecycle ------------------------------------------------------------------
    def shutdown(self) -> None:
        """Stop listening, speaking and the worker threads."""
        self._closed.set()
        self.stop_live()
        self.ptt_release()
        self._token.cancel("shutdown")
        self.player.stop()
        self._confirm_answers.put("")  # unblock a pending confirmation
        self._turns.put(_STOP)
        self._worker.join(timeout=10)
        if self._worker.is_alive():
            logger.warning("Worker thread still busy at shutdown")
        self._health_thread.join(timeout=1)

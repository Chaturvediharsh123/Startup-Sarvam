"""Live speech-to-text over Deepgram's streaming WebSocket (``nova-3``, ``language=hi``).

Same callbacks, events and reliability as :class:`stt.realtime_client.RealtimeSTT`, so
the orchestrator and UI do not change (ADR 0009). The default language is ``hi``: in
live tests ``multi`` often heard short Hindi words as English ("kholo" -> "colo") or
dropped them, while ``hi`` got Hindi right and still kept English words in Roman
letters.

* raw 16-bit PCM is sent as binary frames; ``KeepAlive`` is sent while the mic is
  quiet (Deepgram closes idle streams after about 10 s), ``CloseStream`` on stop;
* Deepgram finalises a sentence in pieces (``is_final``). The pieces are joined and
  delivered as one final when ``speech_final`` arrives (pause of ``endpointing`` ms),
  or on ``UtteranceEnd`` if ``speech_final`` was missed;
* the local silence detector (:class:`stt.vad.LocalSilenceDetector`) is a third backup
  for a stalled connection. It waits ``endpointing + utterance_end_ms + 500`` ms, so it
  never races Deepgram's own signals and cuts a sentence short;
* the language reported is the most common word language of the utterance;
* reconnect with backoff (1 s .. 15 s); ``"fallback"`` after
  ``settings.stt_fallback_after`` failures; a rejected key stops at once.

Protocol: https://developers.deepgram.com/reference/speech-to-text/listen-streaming
Transcripts and the API key are never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlencode

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from core.config import Settings
from core.interfaces import Health
from language.detect import detect_language, from_provider_code, normalise_code
from stt.keyterms import clean_keyterms, keyterm_params
from stt.vad import SILENCE_LEVEL, LocalSilenceDetector

logger = logging.getLogger(__name__)

WS_URL = "wss://api.deepgram.com/v1/listen"
DEFAULT_MODEL = "nova-3"
KEEPALIVE_S = 5.0
MAX_BACKOFF_S = 15.0
UTTERANCE_END_MS = 1000  # Deepgram's minimum; backup when speech_final is missed
LOCAL_BACKUP_MARGIN_MS = 500  # local backup waits for both server signals plus this
LATE_FINAL_S = 3.0  # drop server text this soon after a local final (same utterance)

AudioReader = Callable[[float], bytes | None]


class FatalSTTError(RuntimeError):
    """The server rejected the session in a way a reconnect cannot fix."""


class DeepgramRealtimeSTT:
    """Streams microphone audio and reports partial and final transcripts.

    Args:
        api_key: Deepgram API key.
        settings: ``sample_rate``, ``vad_silence_ms`` (used as ``endpointing``),
            ``local_silence_ms`` (a floor for the local backup), ``stt_fallback_after``,
            ``language_hint``, ``noise_gate``.
        on_partial: called with live text while the user speaks.
        on_final: called with ``(text, language_code)`` when the user pauses.
        on_event: called with ``"connected"``, ``"disconnected"``, ``"fallback"``,
            ``"speech_start"``, ``"speech_end"``, ``"local_end_of_speech"`` or
            ``"error:<message>"``. The end-of-speech events carry the time the last
            word ended as ``"speech_end@<perf_counter>"`` when Deepgram's word timings
            allow it, because they are only known once the final transcript arrives.
        model: Deepgram model.
        language: Deepgram language: ``hi`` (default) or ``multi`` (code-switching).
        endpointing_ms: pause that ends a sentence; defaults to ``settings.vad_silence_ms``.
        keyterms: words to listen for harder, such as app names (see :mod:`stt.keyterms`).
        connect: WebSocket connect function (tests pass a fake).

    Attributes:
        language_hint: our code (e.g. ``hi-IN``) reported when no language is detected.
    """

    def __init__(
        self,
        api_key: str,
        settings: Settings,
        on_partial: Callable[[str], None],
        on_final: Callable[[str, str | None], None],
        on_event: Callable[[str], None] | None = None,
        *,
        model: str = DEFAULT_MODEL,
        language: str = "hi",
        endpointing_ms: int | None = None,
        keyterms: Iterable[str] = (),
        connect: Callable[..., Any] = ws_connect,
    ) -> None:
        if not api_key.strip():
            raise ValueError("DEEPGRAM_API_KEY is empty")
        self._api_key = api_key.strip()
        self.settings = settings
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_event = on_event or (lambda _e: None)
        self.model = model
        self.keyterms = clean_keyterms(keyterms)
        self.language = language
        self.endpointing_ms = endpointing_ms or settings.vad_silence_ms
        self.language_hint = settings.language_hint
        self.connected = False
        self.failures = 0
        self.in_fallback = False
        self.last_error = ""
        self.initial_backoff_s = 1.0
        self.max_backoff_s = MAX_BACKOFF_S
        self._connect = connect
        self.local_silence_ms = max(
            settings.local_silence_ms,
            self.endpointing_ms + UTTERANCE_END_MS + LOCAL_BACKUP_MARGIN_MS,
        )
        self._silence = LocalSilenceDetector(
            self.local_silence_ms,
            max(settings.noise_gate, SILENCE_LEVEL),
            settings.sample_rate,
        )
        self._pieces: list[str] = []
        self._languages: Counter[str] = Counter()
        self._local_final_at = 0.0
        self._last_word_end: float | None = None  # stream seconds, current utterance
        self._audio_sent_s = 0.0  # seconds of audio sent on this connection
        self._timeline: deque[tuple[float, float]] = deque(maxlen=1200)  # (audio s, sent at)

    # -- connection --------------------------------------------------------------
    def url(self) -> str:
        """WebSocket URL with every stream parameter in the query string."""
        params = {
            "model": self.model,
            "language": self.language,
            "encoding": "linear16",
            "sample_rate": self.settings.sample_rate,
            "channels": 1,
            "interim_results": "true",
            "endpointing": self.endpointing_ms,
            "utterance_end_ms": UTTERANCE_END_MS,
            "vad_events": "true",
            "punctuate": "true",
        }
        return f"{WS_URL}?{urlencode([*params.items(), *keyterm_params(self.keyterms)])}"

    def run_blocking(self, read_audio: AudioReader, stop: threading.Event) -> None:
        """Run the client on a fresh event loop until ``stop`` is set (thread target)."""
        asyncio.run(self.run(read_audio, stop))

    async def run(self, read_audio: AudioReader, stop: threading.Event) -> None:
        """Connect, stream and reconnect with backoff until ``stop`` is set."""
        backoff = self.initial_backoff_s
        self.failures = 0
        self.in_fallback = False
        while not stop.is_set():
            try:
                await self._session(read_audio, stop)
                backoff = self.initial_backoff_s
            except FatalSTTError as exc:
                logger.error("Deepgram STT stopped: %s", exc)
                self.last_error = str(exc)
                self.on_event(f"error:{exc}")
                return
            except (TimeoutError, OSError, WebSocketException) as exc:
                logger.warning("Deepgram STT connection lost: %s", type(exc).__name__)
                self.last_error = type(exc).__name__
                self.failures += 1
                self.on_event("disconnected")
                if self.failures >= self.settings.stt_fallback_after and not self.in_fallback:
                    self.in_fallback = True
                    logger.warning("Deepgram STT failed %d times; falling back to batch",
                                   self.failures)
                    self.on_event("fallback")
            if stop.is_set():
                return
            await _sleep_unless(stop, backoff)
            backoff = min(backoff * 2, self.max_backoff_s)

    def _on_connected(self) -> None:
        self.connected = True
        self.failures = 0
        self.in_fallback = False
        self.last_error = ""
        self._silence.reset()
        self._reset_utterance()
        self._audio_sent_s = 0.0  # Deepgram's word times restart on every connection
        self._timeline.clear()
        self.on_event("connected")

    async def _session(self, read_audio: AudioReader, stop: threading.Event) -> None:
        """One WebSocket connection: send audio and receive results concurrently."""
        try:
            ws_cm = self._connect(
                self.url(), additional_headers={"Authorization": f"Token {self._api_key}"}
            )
            async with ws_cm as ws:
                self._on_connected()
                sender = asyncio.create_task(self._send_audio(ws, read_audio, stop))
                receiver = asyncio.create_task(self._receive(ws))
                done, pending = await asyncio.wait(
                    {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
                )
                if sender in done and receiver in pending:
                    # CloseStream was sent: give the last results a moment to arrive.
                    await asyncio.wait({receiver}, timeout=1.0)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    task.result()  # re-raise errors from the finished task
        except InvalidStatus as exc:
            status = exc.response.status_code
            if status in (401, 402, 403):
                raise FatalSTTError(f"API key rejected (HTTP {status})") from exc
            raise
        finally:
            self.connected = False

    async def _send_audio(self, ws: Any, read_audio: AudioReader, stop: threading.Event) -> None:
        """Forward mic chunks as binary frames; KeepAlive when idle; CloseStream on stop."""
        last_sent = time.monotonic()
        while not stop.is_set():
            chunk = await asyncio.to_thread(read_audio, 0.1)
            now = time.monotonic()
            if chunk:
                await ws.send(chunk)
                self._mark_sent(len(chunk), time.perf_counter())
                last_sent = now
                text = self._silence.feed(chunk)
                if text:
                    self._local_final(text)
            elif now - last_sent >= KEEPALIVE_S:
                await ws.send(json.dumps({"type": "KeepAlive"}))
                last_sent = now
        with contextlib.suppress(ConnectionClosed):
            await ws.send(json.dumps({"type": "CloseStream"}))

    async def _receive(self, ws: Any) -> None:
        """Dispatch server messages until the socket closes."""
        async for raw in ws:
            if isinstance(raw, bytes):
                continue
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                logger.debug("Ignoring non-JSON STT message")
                continue
            if isinstance(message, dict):
                self.handle_message(message)

    # -- utterances --------------------------------------------------------------
    def _reset_utterance(self) -> None:
        self._pieces = []
        self._languages = Counter()
        self._last_word_end = None

    # -- when did the user stop speaking? ----------------------------------------------
    def _mark_sent(self, nbytes: int, at: float) -> None:
        """Remember that the stream reached ``_audio_sent_s`` seconds at time ``at``."""
        self._audio_sent_s += nbytes / 2 / self.settings.sample_rate
        self._timeline.append((self._audio_sent_s, at))

    def _wall_time(self, audio_s: float) -> float | None:
        """``perf_counter`` time when stream position ``audio_s`` was sent (about when it
        was spoken), from the chunk that contained it; ``None`` if unknown."""
        for end_s, sent_at in self._timeline:
            if end_s >= audio_s:
                return sent_at - (end_s - audio_s)
        return None

    def _end_event(self, name: str) -> str:
        """``name`` with ``@<time the last word ended>`` when that time is known."""
        if self._last_word_end is None:
            return name
        at = self._wall_time(self._last_word_end)
        return name if at is None else f"{name}@{at:.6f}"

    def _note_word_times(self, alternative: dict[str, Any]) -> None:
        for word in alternative.get("words") or []:
            end = word.get("end") if isinstance(word, dict) else None
            if isinstance(end, int | float) and not isinstance(end, bool):
                self._last_word_end = max(self._last_word_end or 0.0, float(end))

    def _language(self, text: str) -> str | None:
        """The utterance's language.

        With ``multi``: the most common word language, where a tie goes to the
        non-English language (in "Notepad खोलो" the English word is an app name and
        the sentence is Hindi). With one language (``hi``) Deepgram tags no words and
        English sentences come back too, so the language is detected from the text
        ("Open Chrome." -> ``en-IN``). The hint is the last resort.
        """
        if self._languages:
            best = max(self._languages.items(), key=lambda kv: (kv[1], kv[0] != "en"))
            return from_provider_code(best[0])
        hint = normalise_code(self.language_hint)
        if self.language != "multi" and text.strip():
            return detect_language(text, default=hint or "hi-IN").code
        return hint

    def _deliver(self) -> None:
        """Send the joined final pieces as one final transcript."""
        text = " ".join(self._pieces).strip()
        language = self._language(text)
        self._reset_utterance()
        self._silence.on_final()
        if text:
            self.on_final(text, language)

    def _local_final(self, text: str) -> None:
        """Deepgram's end of speech is late: use the last partial as the final."""
        logger.info("Local silence timeout: using last partial as final")
        language = self._language(text)
        event = self._end_event("local_end_of_speech")
        self._reset_utterance()
        self._local_final_at = time.monotonic()
        self.on_event(event)
        self.on_final(text, language)

    def _late(self) -> bool:
        """True while server text may still belong to an utterance ended locally."""
        return self._local_final_at > 0 and time.monotonic() - self._local_final_at < LATE_FINAL_S

    def handle_message(self, message: dict[str, Any]) -> None:
        """React to one server message (public so tests can drive it directly)."""
        kind = message.get("type")
        if kind == "Results":
            self._on_results(message)
        elif kind == "UtteranceEnd":
            if self._late():
                self._local_final_at = 0.0
                self._reset_utterance()
                return
            if self._pieces:
                self.on_event(self._end_event("speech_end"))
                self._deliver()
        elif kind == "SpeechStarted":
            self._local_final_at = 0.0
            self.on_event("speech_start")
        elif kind == "Error":
            logger.error("Deepgram error %s: %s", message.get("code"), message.get("description"))
        elif kind == "Metadata":
            logger.info("Deepgram stream started (%s)", message.get("request_id"))

    def _on_results(self, message: dict[str, Any]) -> None:
        try:
            alternative = message["channel"]["alternatives"][0]
        except (KeyError, IndexError, TypeError):
            return
        text = str(alternative.get("transcript") or "").strip()
        if self._late():
            if message.get("speech_final"):
                self._local_final_at = 0.0
                self._reset_utterance()
            return
        if text:
            self._note_word_times(alternative)
        if message.get("is_final"):
            if text:
                self._pieces.append(text)
                for word in alternative.get("words") or []:
                    if isinstance(word, dict) and word.get("language"):
                        self._languages[str(word["language"])] += 1
                if not alternative.get("words"):
                    for language in alternative.get("languages") or []:
                        self._languages[str(language)] += 1
                        break
            if message.get("speech_final"):
                if self._pieces:  # Deepgram also sends empty finals after silence
                    self.on_event(self._end_event("speech_end"))
                self._deliver()
                return
            live = " ".join(self._pieces)
        else:
            live = " ".join([*self._pieces, text]).strip()
        if live:
            self._silence.on_partial(live)
            self.on_partial(live)

    def health(self) -> Health:
        """Status-bar health: green while connected."""
        if self.connected:
            return Health("stt", True, "live")
        if self.in_fallback:
            return Health("stt", False, f"push-to-talk fallback ({self.last_error})")
        if self.last_error:
            return Health("stt", False, f"reconnecting ({self.last_error})")
        return Health("stt", True, "idle")


async def _sleep_unless(stop: threading.Event, seconds: float) -> None:
    """Sleep up to ``seconds``, waking early when ``stop`` is set."""
    end = time.monotonic() + seconds
    while not stop.is_set() and time.monotonic() < end:
        await asyncio.sleep(0.1)

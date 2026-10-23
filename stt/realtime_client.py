"""Live speech-to-text over Sarvam's realtime WebSocket.

Endpoint, parameters and messages follow docs/sarvam_api_notes.md, section 1:
``wss://api.sarvam.ai/speech-to-text-realtime/ws``, base64 ``linear16`` audio in
``audio_input`` events, server-side VAD that emits ``transcript.final`` at the end
of each utterance, ``ping`` keepalive, and ``end`` for a graceful close.

Reliability:

* reconnect with exponential backoff (1 s .. 15 s) until ``stop`` is set;
* after ``settings.stt_fallback_after`` consecutive failed attempts, emit
  ``"fallback"`` (the orchestrator switches to push-to-talk with the batch
  client) and keep retrying in the background; ``"connected"`` means it is back;
* a local silence timeout (:class:`stt.vad.LocalSilenceDetector`,
  ``settings.local_silence_ms``) turns the last partial into a final if the
  server's end-of-speech never arrives.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from core.config import Settings
from core.interfaces import Health
from language.detect import normalise_code, to_realtime_stt_code
from sarvam.client import SarvamClient
from stt.vad import SILENCE_LEVEL, LocalSilenceDetector

logger = logging.getLogger(__name__)

WS_PATH = "/speech-to-text-realtime/ws"
FATAL_CLOSE_CODES = frozenset({1003, 4000})  # bad key / quota, or invalid parameter
KEEPALIVE_S = 5.0
MAX_BACKOFF_S = 15.0
LATE_FINAL_S = 3.0  # drop a server final this soon after a local one (same utterance)

AudioReader = Callable[[float], bytes | None]


class FatalSTTError(RuntimeError):
    """The server rejected the session in a way a reconnect cannot fix."""


class RealtimeSTT:
    """Streams microphone audio and reports partial and final transcripts.

    Args:
        client: provides the auth header and WebSocket URL.
        settings: model, sample rate, VAD silence, ``local_silence_ms``,
            ``stt_fallback_after``, ``language_hint`` and ``noise_gate``.
        on_partial: called with live text while the user speaks.
        on_final: called with ``(text, language_code)`` when the user pauses.
        on_event: called with ``"connected"``, ``"disconnected"``, ``"fallback"``,
            ``"speech_start"``, ``"speech_end"``, ``"local_end_of_speech"`` or
            ``"error:<message>"`` for status, timing and barge-in.
        connect: WebSocket connect function (tests pass a fake).

    Attributes:
        language_hint: language code to request instead of ``auto`` (e.g. ``hi-IN``);
            empty = auto-detect. Takes effect on the next connection.
        initial_backoff_s / max_backoff_s: reconnect delay, doubled after each failure.
    """

    def __init__(
        self,
        client: SarvamClient,
        settings: Settings,
        on_partial: Callable[[str], None],
        on_final: Callable[[str, str | None], None],
        on_event: Callable[[str], None] | None = None,
        connect: Callable[..., Any] = ws_connect,
    ) -> None:
        self.client = client
        self.settings = settings
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_event = on_event or (lambda _e: None)
        self.language_hint = settings.language_hint
        self.connected = False
        self.failures = 0
        self.in_fallback = False
        self.last_error = ""
        self.initial_backoff_s = 1.0
        self.max_backoff_s = MAX_BACKOFF_S
        self._connect = connect
        self._silence = LocalSilenceDetector(
            settings.local_silence_ms,
            max(settings.noise_gate, SILENCE_LEVEL),
            settings.sample_rate,
        )
        self._utterance: Any = None
        self._partial_language: str | None = None
        self._closed_utterances: deque[Any] = deque(maxlen=8)
        self._local_final_at = 0.0

    # -- connection --------------------------------------------------------------
    def request_language(self) -> str:
        """The ``language_code`` query value: the hint, or ``auto``."""
        code = normalise_code(self.language_hint)
        return to_realtime_stt_code(code) if code else "auto"

    def url(self) -> str:
        """WebSocket URL with every connection parameter in the query string."""
        return self.client.ws_url(
            WS_PATH,
            {
                "language_code": self.request_language(),
                "model": self.settings.stt_realtime_model,
                "stream_type": "fast",
                "mode": "transcribe",
                "endpointing": "vad",
                "encoding": "linear16",
                "sample_rate": self.settings.sample_rate,
                "silence_duration_ms": self.settings.vad_silence_ms,
            },
        )

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
                logger.error("Realtime STT stopped: %s", exc)
                self.last_error = str(exc)
                self.on_event(f"error:{exc}")
                return
            except (TimeoutError, OSError, WebSocketException) as exc:
                logger.warning("Realtime STT connection lost: %s", exc)
                self.last_error = str(exc)
                self.failures += 1
                self.on_event("disconnected")
                if self.failures >= self.settings.stt_fallback_after and not self.in_fallback:
                    self.in_fallback = True
                    logger.warning("Realtime STT failed %d times; falling back to batch",
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
        self._closed_utterances.clear()
        self._utterance = None
        self.on_event("connected")

    async def _session(self, read_audio: AudioReader, stop: threading.Event) -> None:
        """One WebSocket connection: send audio and receive events concurrently."""
        try:
            ws_cm = self._connect(self.url(), additional_headers=self.client.auth_headers)
            async with ws_cm as ws:
                self._on_connected()
                sender = asyncio.create_task(self._send_audio(ws, read_audio, stop))
                receiver = asyncio.create_task(self._receive(ws))
                done, pending = await asyncio.wait(
                    {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    task.result()  # re-raise errors from the finished task
        except InvalidStatus as exc:
            status = exc.response.status_code
            if status in (401, 403):
                raise FatalSTTError(f"API key rejected (HTTP {status})") from exc
            raise
        except ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd else None
            if code in FATAL_CLOSE_CODES:
                reason = exc.rcvd.reason if exc.rcvd else ""
                raise FatalSTTError(f"server closed the connection ({code} {reason})") from exc
            raise
        finally:
            self.connected = False

    async def _send_audio(self, ws: Any, read_audio: AudioReader, stop: threading.Event) -> None:
        """Forward mic chunks; ping when idle; send ``end`` when asked to stop."""
        last_sent = time.monotonic()
        while not stop.is_set():
            chunk = await asyncio.to_thread(read_audio, 0.1)
            now = time.monotonic()
            if chunk:
                audio = base64.b64encode(chunk).decode("ascii")
                await ws.send(json.dumps({"event": "audio_input", "audio": audio}))
                last_sent = now
                text = self._silence.feed(chunk)
                if text:
                    self._local_final(text)
            elif now - last_sent >= KEEPALIVE_S:
                await ws.send(json.dumps({"event": "ping"}))
                last_sent = now
        await ws.send(json.dumps({"event": "end"}))
        await asyncio.sleep(0.2)  # let session.end arrive
        await ws.close()

    async def _receive(self, ws: Any) -> None:
        """Dispatch server events until the socket closes."""
        async for raw in ws:
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                logger.debug("Ignoring non-JSON STT message")
                continue
            self.handle_message(message)

    # -- end of speech -----------------------------------------------------------
    def _local_final(self, text: str) -> None:
        """The server's end-of-speech is late: use the last partial as the final."""
        logger.info("Local silence timeout: using last partial as final (%r)", text)
        if self._utterance is not None:
            self._closed_utterances.append(self._utterance)
        self._local_final_at = time.monotonic()
        self.on_event("local_end_of_speech")
        self.on_final(text, self._partial_language or normalise_code(self.language_hint))

    def _already_final(self, utterance: Any) -> bool:
        """True for messages of an utterance the local timeout already finished."""
        if utterance is not None:
            return utterance in self._closed_utterances
        return time.monotonic() - self._local_final_at < LATE_FINAL_S

    def handle_message(self, message: dict[str, Any]) -> None:
        """React to one server event (public so tests can drive it directly)."""
        event = message.get("event")
        utterance = message.get("utterance_idx")
        if event == "transcript.partial":
            text = str(message.get("text") or "").strip()
            if text and not self._already_final(utterance):
                self._utterance = utterance
                self._partial_language = message.get("language") or self._partial_language
                self._silence.on_partial(text)
                self.on_partial(text)
        elif event == "transcript.final":
            text = str(message.get("text") or "").strip()
            if self._already_final(utterance):
                logger.info("Dropping late server final (already ended locally): %r", text)
                self._local_final_at = 0.0
                return
            self._silence.on_final()
            if text:
                language = message.get("language") or normalise_code(self.language_hint)
                self.on_final(text, language)
        elif event == "vad.speech_start":
            self._local_final_at = 0.0
            self.on_event("speech_start")
        elif event == "vad.speech_end":
            self.on_event("speech_end")
        elif event == "error":
            logger.error("STT error %s: %s", message.get("code"), message.get("message"))
            if message.get("is_fatal"):
                raise FatalSTTError(f"{message.get('code')}: {message.get('message')}")
        elif event == "session.end":
            logger.info("STT session ended, billed audio %.1fs", message.get("audio_duration_s", 0))
        elif event == "session.begin":
            logger.info("STT session started (%s)", message.get("request_id"))

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

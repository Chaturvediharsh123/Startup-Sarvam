"""Live speech-to-text over Sarvam's realtime WebSocket.

Endpoint, parameters and messages follow docs/sarvam_api_notes.md, section 1:
``wss://api.sarvam.ai/speech-to-text-realtime/ws``, base64 ``linear16`` audio in
``audio_input`` events, server-side VAD that emits ``transcript.final`` at the end
of each utterance, ``ping`` keepalive, and ``end`` for a graceful close.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from core.config import Settings
from sarvam.client import SarvamClient

logger = logging.getLogger(__name__)

WS_PATH = "/speech-to-text-realtime/ws"
FATAL_CLOSE_CODES = frozenset({1003, 4000})  # bad key / quota, or invalid parameter
KEEPALIVE_S = 5.0
MAX_BACKOFF_S = 15.0

AudioReader = Callable[[float], bytes | None]


class FatalSTTError(RuntimeError):
    """The server rejected the session in a way a reconnect cannot fix."""


class RealtimeSTT:
    """Streams microphone audio and reports partial and final transcripts.

    Args:
        client: provides the auth header and WebSocket URL.
        settings: model, sample rate and VAD silence settings.
        on_partial: called with live text while the user speaks.
        on_final: called with ``(text, language_code)`` when the user pauses.
        on_event: called with ``"speech_start"``, ``"speech_end"``, ``"connected"``,
            ``"disconnected"`` or ``"error:<message>"`` for status, timing and barge-in.
        connect: WebSocket connect function (tests pass a fake).
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
        self._connect = connect

    def url(self) -> str:
        """WebSocket URL with every connection parameter in the query string."""
        return self.client.ws_url(
            WS_PATH,
            {
                "language_code": "auto",
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
        backoff = 1.0
        while not stop.is_set():
            try:
                await self._session(read_audio, stop)
                backoff = 1.0
            except FatalSTTError as exc:
                logger.error("Realtime STT stopped: %s", exc)
                self.on_event(f"error:{exc}")
                return
            except (TimeoutError, OSError, WebSocketException) as exc:
                logger.warning("Realtime STT connection lost: %s", exc)
                self.on_event("disconnected")
            if stop.is_set():
                return
            await _sleep_unless(stop, backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)

    async def _session(self, read_audio: AudioReader, stop: threading.Event) -> None:
        """One WebSocket connection: send audio and receive events concurrently."""
        try:
            ws_cm = self._connect(self.url(), additional_headers=self.client.auth_headers)
            async with ws_cm as ws:
                self.on_event("connected")
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

    def handle_message(self, message: dict[str, Any]) -> None:
        """React to one server event (public so tests can drive it directly)."""
        event = message.get("event")
        if event == "transcript.partial":
            text = str(message.get("text") or "").strip()
            if text:
                self.on_partial(text)
        elif event == "transcript.final":
            text = str(message.get("text") or "").strip()
            if text:
                self.on_final(text, message.get("language"))
        elif event == "vad.speech_start":
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


async def _sleep_unless(stop: threading.Event, seconds: float) -> None:
    """Sleep up to ``seconds``, waking early when ``stop`` is set."""
    end = time.monotonic() + seconds
    while not stop.is_set() and time.monotonic() < end:
        await asyncio.sleep(0.1)

"""Cartesia text-to-speech (``POST /tts/bytes``, ``sonic-3.6``) with sentence pipelining.

Each sentence is one request with ``output_format`` raw ``pcm_s16le``; the response body
streams while audio is generated, so chunks are played as they arrive. Like
:class:`tts.stream_client.SarvamTTS`, the reply is cleaned for speech, split into
sentences, and downloaded by a background thread into a :class:`SpeechStream`, so
sentence N+1 is generated while sentence N plays and Interrupt stops both at once.

API: https://docs.cartesia.ai/api-reference/tts/bytes (ADR 0009).
The API key and the spoken text are never logged.
"""

from __future__ import annotations

import logging
import threading
import time
import weakref
from collections.abc import Callable
from typing import Any

import httpx

from core.interfaces import Health
from language.detect import to_cartesia_language
from tts.sentence_splitter import split_sentences
from tts.stream_client import PREFETCH_CHUNKS, SpeechStream, _pcm_chunks
from tts.text_cleaner import clean_for_speech

logger = logging.getLogger(__name__)

BASE_URL = "https://api.cartesia.ai"
BYTES_PATH = "/tts/bytes"
API_VERSION = "2026-08-14"
DEFAULT_MODEL = "sonic-3.6"
SAMPLE_RATES = frozenset({8000, 16000, 22050, 24000, 44100, 48000})
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class CartesiaError(RuntimeError):
    """A Cartesia request failed.

    Attributes:
        status: HTTP status code (0 for network errors and timeouts).
    """

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status

    @property
    def retryable(self) -> bool:
        """True for network errors, rate limits and server errors."""
        return self.status == 0 or self.status in RETRY_STATUSES


class CartesiaTTS:
    """Turns reply text into a stream of PCM chunks (implements ``TextToSpeech``).

    Args:
        api_key: Cartesia API key (``sk_car_...``).
        voice_id: default voice for every language.
        voices: optional per-language voices, keyed by Cartesia code (``{"hi": "..."}``).
        model: Cartesia model id; pin a dated snapshot in production.
        sample_rate: output rate in Hz, one of :data:`SAMPLE_RATES`.
        speed: 0.6 .. 1.5 (1.0 = normal).
        default_language: Cartesia code used when the reply has no language.
        clean: run :func:`tts.text_cleaner.clean_for_speech` on every reply.
    """

    def __init__(
        self,
        api_key: str,
        voice_id: str,
        *,
        voices: dict[str, str] | None = None,
        model: str = DEFAULT_MODEL,
        sample_rate: int = 24000,
        speed: float = 1.0,
        default_language: str = "hi",
        timeout_s: float = 15.0,
        prefetch_chunks: int = PREFETCH_CHUNKS,
        clean: bool = True,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key.strip():
            raise ValueError("CARTESIA_API_KEY is empty")
        if not voice_id.strip():
            raise ValueError("a Cartesia voice id is required")
        if sample_rate not in SAMPLE_RATES:
            raise ValueError(f"sample_rate must be one of {sorted(SAMPLE_RATES)}")
        if not 0.6 <= speed <= 1.5:
            raise ValueError("speed must be between 0.6 and 1.5")
        self.voice_id = voice_id.strip()
        self.voices = dict(voices or {})
        self.model = model
        self.sample_rate = sample_rate
        self.speed = speed
        self.default_language = default_language
        self.prefetch_chunks = prefetch_chunks
        self.clean = clean
        self.last_error = ""
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key.strip()}",
                "Cartesia-Version": API_VERSION,
            },
            timeout=timeout_s,
            transport=transport,
        )
        self._streams: weakref.WeakSet[SpeechStream] = weakref.WeakSet()
        self._lock = threading.Lock()

    def payload(self, text: str, language: str | None) -> dict[str, Any]:
        """Request body for one sentence."""
        cartesia_language = to_cartesia_language(language, self.default_language)
        body: dict[str, Any] = {
            "model_id": self.model,
            "transcript": text,
            "voice": {"id": self.voices.get(cartesia_language, self.voice_id)},
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": self.sample_rate,
            },
            "language": cartesia_language,
        }
        if self.speed != 1.0:
            body["generation_config"] = {"speed": self.speed}
        return body

    def stream(self, text: str, language: str | None) -> SpeechStream:
        """PCM chunks for ``text``; sentence N+1 downloads while sentence N plays.

        Iterating raises :class:`CartesiaError` if a sentence fails before any audio.
        """
        if self.clean:
            text = clean_for_speech(text, language)
        sentences = split_sentences(text)
        stream = SpeechStream(
            lambda job: self._produce(job, sentences, language), self.prefetch_chunks
        )
        with self._lock:
            self._streams.add(stream)
        return stream

    def cancel(self) -> None:
        """Cancel every stream in progress (Interrupt)."""
        with self._lock:
            streams = list(self._streams)
        for stream in streams:
            stream.cancel()

    def _produce(self, job: SpeechStream, sentences: list[str], language: str | None) -> None:
        """Producer thread: speak each sentence in order into ``job``."""
        try:
            for sentence in sentences:
                if job.cancelled or not self._speak_sentence(job, sentence, language):
                    return
        except Exception as exc:
            if not job.cancelled:
                self.last_error = str(exc)
            raise
        self.last_error = ""

    def _speak_sentence(
        self, job: SpeechStream, sentence: str, language: str | None, attempts: int = 2
    ) -> bool:
        """Stream one sentence into ``job``; False once the job was cancelled.

        Retries once when the request fails before any audio arrived. A stream that
        breaks mid-sentence is logged and skipped, so a half-spoken sentence is not
        repeated.
        """
        body = self.payload(sentence, language)
        for attempt in range(1, attempts + 1):
            produced = False
            start = time.perf_counter()
            first_ms: float | None = None
            size = 0
            try:
                with self._http.stream("POST", BYTES_PATH, json=body) as response:
                    headers_ms = (time.perf_counter() - start) * 1000
                    if response.status_code != 200:
                        response.read()
                        raise CartesiaError(_describe(response), response.status_code)
                    job.add_cancel_hook(response.close)
                    try:
                        for pcm in _pcm_chunks(response.iter_bytes()):
                            if first_ms is None:
                                first_ms = (time.perf_counter() - start) * 1000
                            produced = True
                            size += len(pcm)
                            if not job.put(pcm):
                                return False
                    finally:
                        job.remove_cancel_hook(response.close)
                logger.info(
                    "Cartesia sentence (%d chars, %s): headers %.0f ms, first audio %s ms, "
                    "%.1f s audio, %.0f ms total",
                    len(sentence), body["language"], headers_ms,
                    f"{first_ms:.0f}" if first_ms is not None else "-",
                    size / 2 / self.sample_rate, (time.perf_counter() - start) * 1000,
                )
                return not job.cancelled
            except (CartesiaError, httpx.HTTPError) as exc:
                if job.cancelled:
                    return False
                error = exc if isinstance(exc, CartesiaError) else CartesiaError(
                    f"network error: {type(exc).__name__}"
                )
                if produced:
                    logger.error("TTS stream broke mid-sentence: %s", error)
                    return True
                if not error.retryable or attempt == attempts:
                    raise error from None
                logger.warning("Cartesia request failed (%s), retrying", error)
                self._sleep(0.3 * attempt)
        return False

    def health(self) -> Health:
        """Red after the last reply failed."""
        if self.last_error:
            return Health("tts", False, self.last_error)
        return Health("tts", True, "ready")

    def close(self) -> None:
        """Cancel streams in progress and close the HTTP connection pool."""
        self.cancel()
        self._http.close()


def _describe(response: httpx.Response) -> str:
    """Short error text from a Cartesia error body, without the request data."""
    message = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = body.get("error") or body.get("message") or body.get("detail") or ""
            message = detail if isinstance(detail, str) else str(detail)
    except ValueError:
        message = response.text
    if response.status_code == 401:
        message = message or "invalid API key"
    return f"HTTP {response.status_code}: {message[:200]}".rstrip(": ")

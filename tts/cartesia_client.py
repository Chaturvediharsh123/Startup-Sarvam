"""Cartesia text-to-speech (``POST /tts/bytes``, ``sonic-3.6``) with sentence pipelining.

Each sentence is one request with ``output_format`` raw ``pcm_s16le``; the response body
streams while audio is generated, so chunks are played as they arrive. Like
:class:`tts.stream_client.SarvamTTS`, the reply is cleaned for speech, split into
sentences, and downloaded by a background thread into a :class:`SpeechStream`, so
sentence N+1 is generated while sentence N plays and Interrupt stops both at once.

Latency (live tests: Cartesia sometimes waits 2.4-4 s before answering):

* **cache:** short sentences (fixed replies such as "Notepad khul gaya") are kept in
  memory after the first time and replayed instantly (LRU, ``cache_entries``);
* **hedged request:** if no response has started after ``hedge_after_s``, the same
  request is sent again in parallel and whichever answers first is played; the
  other is closed. A failed request is retried once the same way.

API: https://docs.cartesia.ai/api-reference/tts/bytes (ADR 0009).
The API key and the spoken text are never logged.
"""

from __future__ import annotations

import logging
import threading
import time
import weakref
from collections import OrderedDict
from collections.abc import Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
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
HEDGE_AFTER_S = 1.2
CACHE_ENTRIES = 64
CACHE_MAX_CHARS = 80
_CHUNK_S = 0.1  # cached audio is replayed in ~100 ms chunks


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
        hedge_after_s: send a second request if the first has not answered by then
            (0 = never). The abandoned request may still be billed.
        cache_entries: how many short sentences to keep (0 = no cache).
        cache_max_chars: only sentences up to this length are cached.
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
        hedge_after_s: float = HEDGE_AFTER_S,
        cache_entries: int = CACHE_ENTRIES,
        cache_max_chars: int = CACHE_MAX_CHARS,
        transport: httpx.BaseTransport | None = None,
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
        self.hedge_after_s = hedge_after_s
        self.cache_entries = cache_entries
        self.cache_max_chars = cache_max_chars
        self.last_error = ""
        self._cache: OrderedDict[tuple[Any, ...], bytes] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cartesia")
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

    def _speak_sentence(self, job: SpeechStream, sentence: str, language: str | None) -> bool:
        """Speak one sentence into ``job``; False once the job was cancelled.

        Plays from the cache when possible. A stream that breaks mid-sentence is
        logged and skipped, so a half-spoken sentence is not repeated.
        """
        body = self.payload(sentence, language)
        key = self._cache_key(body)
        cached = self._cache_get(key)
        if cached is not None:
            logger.info("Cartesia sentence (%d chars, %s): from cache", len(sentence),
                        body["language"])
            return all(job.put(chunk) for chunk in self._chunks(cached))
        start = time.perf_counter()
        response = self._open(body, job)
        if response is None:
            return False
        headers_ms = (time.perf_counter() - start) * 1000
        first_ms: float | None = None
        audio: list[bytes] = []
        job.add_cancel_hook(response.close)
        try:
            for pcm in _pcm_chunks(response.iter_bytes()):
                if first_ms is None:
                    first_ms = (time.perf_counter() - start) * 1000
                audio.append(pcm)
                if not job.put(pcm):
                    return False
        except httpx.HTTPError as exc:
            if job.cancelled:
                return False
            if audio:
                logger.error("TTS stream broke mid-sentence: %s", type(exc).__name__)
                return True
            raise CartesiaError(f"network error: {type(exc).__name__}") from None
        finally:
            job.remove_cancel_hook(response.close)
            response.close()
        if job.cancelled:
            return False
        if key is not None and audio:
            self._cache_put(key, b"".join(audio))
        logger.info(
            "Cartesia sentence (%d chars, %s): headers %.0f ms, first audio %s ms, "
            "%.1f s audio, %.0f ms total",
            len(sentence), body["language"], headers_ms,
            f"{first_ms:.0f}" if first_ms is not None else "-",
            sum(map(len, audio)) / 2 / self.sample_rate, (time.perf_counter() - start) * 1000,
        )
        return True

    # -- requests ----------------------------------------------------------------
    def _request(self, body: dict[str, Any]) -> httpx.Response:
        """Send one request and wait for its headers; raises :class:`CartesiaError`."""
        try:
            response = self._http.send(
                self._http.build_request("POST", BYTES_PATH, json=body), stream=True
            )
        except httpx.HTTPError as exc:
            raise CartesiaError(f"network error: {type(exc).__name__}") from None
        if response.status_code != 200:
            try:
                response.read()
                raise CartesiaError(_describe(response), response.status_code)
            finally:
                response.close()
        return response

    def _open(self, body: dict[str, Any], job: SpeechStream) -> httpx.Response | None:
        """An answered response for ``body``, sending a second request if needed.

        A second request is sent when the first has not answered after
        ``hedge_after_s``, or failed with a retryable error. Whichever answers first
        wins; the other is closed. Returns ``None`` if ``job`` was cancelled.
        """
        first = self._pool.submit(self._request, body)
        pending: list[Future[httpx.Response]] = [first]
        sent = 1
        started = time.perf_counter()
        last_error: CartesiaError | None = None
        while True:
            if job.cancelled:
                _discard(pending)
                return None
            done, _ = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
            for future in done:
                pending.remove(future)
                error = future.exception()
                if error is None:
                    _discard(pending)
                    if sent > 1:
                        logger.info("Cartesia: the %s request answered first",
                                    "first" if future is first else "second")
                    return future.result()
                last_error = error if isinstance(error, CartesiaError) else CartesiaError(
                    f"unexpected error: {type(error).__name__}"
                )
                if not last_error.retryable:
                    _discard(pending)
                    raise last_error
            if sent == 1 and (
                last_error is not None
                or (self.hedge_after_s > 0
                    and time.perf_counter() - started >= self.hedge_after_s)
            ):
                if last_error is not None:
                    logger.warning("Cartesia request failed (%s), retrying", last_error)
                else:
                    logger.info("Cartesia slow (no answer after %.1f s), sending a second "
                                "request", self.hedge_after_s)
                pending.append(self._pool.submit(self._request, body))
                sent = 2
            if not pending:
                assert last_error is not None
                raise last_error

    # -- cache -------------------------------------------------------------------
    def _cache_key(self, body: dict[str, Any]) -> tuple[Any, ...] | None:
        """Cache key for a short sentence, else ``None`` (not cached)."""
        text = body["transcript"]
        if self.cache_entries <= 0 or len(text) > self.cache_max_chars:
            return None
        return (text, body["language"], body["voice"]["id"], self.model, self.sample_rate,
                self.speed)

    def _cache_get(self, key: tuple[Any, ...] | None) -> bytes | None:
        if key is None:
            return None
        with self._cache_lock:
            audio = self._cache.get(key)
            if audio is not None:
                self._cache.move_to_end(key)
            return audio

    def _cache_put(self, key: tuple[Any, ...], audio: bytes) -> None:
        with self._cache_lock:
            self._cache[key] = audio
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_entries:
                self._cache.popitem(last=False)

    def _chunks(self, audio: bytes) -> Iterator[bytes]:
        """Split cached PCM into ~100 ms chunks so playback can be interrupted."""
        step = int(self.sample_rate * _CHUNK_S) * 2
        for offset in range(0, len(audio), step):
            yield audio[offset : offset + step]

    def health(self) -> Health:
        """Red after the last reply failed."""
        if self.last_error:
            return Health("tts", False, self.last_error)
        return Health("tts", True, "ready")

    def close(self) -> None:
        """Cancel streams in progress and close the HTTP connection pool."""
        self.cancel()
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._http.close()


def _discard(futures: list[Future[httpx.Response]]) -> None:
    """Close the responses of requests nobody is waiting for any more."""
    for future in futures:
        future.add_done_callback(_close_result)


def _close_result(future: Future[httpx.Response]) -> None:
    if not future.cancelled() and future.exception() is None:
        future.result().close()


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

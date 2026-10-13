"""Push-to-talk speech-to-text with Deepgram (``POST /v1/listen``, pre-recorded audio).

Sends one WAV recording to ``nova-3`` with ``language=multi`` so English, Hindi and
Hinglish code-switching are all transcribed in one request (ADR 0009). The response
lists the detected languages by word count; the first one is reported.

Request and response shapes: https://developers.deepgram.com/docs/multilingual-code-switching
Transcripts and the API key are never logged.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from typing import Any

import httpx

from core.interfaces import Health
from language.detect import from_provider_code, normalise_code
from stt.batch_client import pcm_to_wav
from stt.keyterms import clean_keyterms, keyterm_params

logger = logging.getLogger(__name__)

LISTEN_URL = "https://api.deepgram.com/v1/listen"
DEFAULT_MODEL = "nova-3"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MIN_SECONDS = 0.2


class DeepgramError(RuntimeError):
    """A Deepgram request failed.

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


def parse_response(body: Any) -> tuple[str, str | None]:
    """Return ``(transcript, provider_language)`` from a ``/v1/listen`` JSON body."""
    try:
        alternative = body["results"]["channels"][0]["alternatives"][0]
    except (KeyError, IndexError, TypeError):
        return "", None
    text = str(alternative.get("transcript") or "").strip()
    languages = alternative.get("languages") or []
    language = languages[0] if languages and isinstance(languages[0], str) else None
    return text, language


class DeepgramBatchSTT:
    """Transcribes one recording and detects its language (implements ``BatchSpeechToText``).

    Args:
        keyterms: words to listen for harder, such as app names (see :mod:`stt.keyterms`).

    Attributes:
        language_hint: our code (e.g. ``hi-IN``) reported when Deepgram detects no language.
    """

    def __init__(
        self,
        api_key: str,
        language_hint: str = "",
        model: str = DEFAULT_MODEL,
        keyterms: Iterable[str] = (),
        timeout_s: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key.strip():
            raise ValueError("DEEPGRAM_API_KEY is empty")
        self.language_hint = language_hint
        self.model = model
        self.keyterms = clean_keyterms(keyterms)
        self.last_error = ""
        self._sleep = sleep
        self._http = httpx.Client(
            headers={"Authorization": f"Token {api_key.strip()}"},
            timeout=timeout_s,
            transport=transport,
        )

    def transcribe(self, pcm: bytes, sample_rate: int) -> tuple[str, str | None]:
        """Return ``(transcript, language_code)`` for 16-bit mono PCM audio.

        Recordings under 0.2 s are skipped without a request. Raises
        :class:`DeepgramError` when the request fails after one retry.
        """
        hint = normalise_code(self.language_hint)
        if len(pcm) < int(MIN_SECONDS * sample_rate) * 2:
            return "", None
        try:
            body = self._post(pcm_to_wav(pcm, sample_rate))
        except DeepgramError as exc:
            self.last_error = str(exc)
            raise
        self.last_error = ""
        text, language = parse_response(body)
        return text, from_provider_code(language) or hint

    def _post(self, wav: bytes, attempts: int = 2) -> Any:
        params = [("model", self.model), ("language", "multi"), ("punctuate", "true"),
                  *keyterm_params(self.keyterms)]
        for attempt in range(1, attempts + 1):
            try:
                response = self._http.post(
                    LISTEN_URL, params=params, content=wav,
                    headers={"Content-Type": "audio/wav"},
                )
            except httpx.HTTPError as exc:
                error = DeepgramError(f"network error: {type(exc).__name__}")
            else:
                if response.status_code == 200:
                    return response.json()
                error = DeepgramError(_describe(response), response.status_code)
            if not error.retryable or attempt == attempts:
                raise error
            logger.warning("Deepgram request failed (%s), retrying", error)
            self._sleep(0.5 * attempt)
        raise AssertionError("unreachable")

    def health(self) -> Health:
        """Red after the last request failed."""
        if self.last_error:
            return Health("stt_batch", False, self.last_error)
        return Health("stt_batch", True, "ready")

    def close(self) -> None:
        """Close the HTTP connection pool."""
        self._http.close()


def _describe(response: httpx.Response) -> str:
    """Short error text from a Deepgram error body (``err_msg``), without request data."""
    message = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            message = str(body.get("err_msg") or body.get("message") or "")
    except ValueError:
        pass
    if response.status_code == 401:
        message = message or "invalid API key"
    return f"HTTP {response.status_code}: {message[:200]}".rstrip(": ")

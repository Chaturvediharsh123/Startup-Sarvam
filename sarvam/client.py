"""Shared HTTP client for every Sarvam REST call.

One ``httpx.Client`` holds the ``api-subscription-key`` header, timeouts and a
connection pool. Requests that fail with HTTP 429 or 5xx are retried with
exponential backoff; other errors raise :class:`SarvamError` straight away.
See docs/sarvam_api_notes.md ("Base URL and authentication").
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://api.sarvam.ai"
WS_BASE_URL = "wss://api.sarvam.ai"
AUTH_HEADER = "api-subscription-key"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class SarvamError(RuntimeError):
    """A Sarvam API call failed.

    Attributes:
        status: HTTP status code (0 for network errors).
        code: Sarvam error code such as ``invalid_api_key_error``, if known.
        request_id: Sarvam request id, useful for support tickets.
    """

    def __init__(
        self, message: str, status: int = 0, code: str | None = None, request_id: str | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.request_id = request_id

    @property
    def retryable(self) -> bool:
        """True for rate limits and server errors."""
        return self.status in RETRY_STATUSES


def error_from_response(response: httpx.Response) -> SarvamError:
    """Build a :class:`SarvamError` from a Sarvam error body ``{"error": {...}}``."""
    message, code, request_id = response.text[:300], None, None
    try:
        body = response.json()
        detail = body.get("error", body) if isinstance(body, dict) else {}
        if isinstance(detail, dict):
            message = str(detail.get("message") or message)
            code = detail.get("code")
            request_id = detail.get("request_id")
    except ValueError:
        pass
    if response.status_code == 403:
        message = f"{message} (check SARVAM_API_KEY)"
    return SarvamError(
        f"HTTP {response.status_code}: {message}", response.status_code, code, request_id
    )


class SarvamClient:
    """Thin wrapper around ``httpx.Client`` with Sarvam auth, timeouts and retries.

    Args:
        api_key: Sarvam API subscription key.
        timeout: seconds for connect/read/write.
        retries: extra attempts after the first for 429/5xx responses.
        transport: optional ``httpx`` transport (tests use ``httpx.MockTransport``).
    """

    def __init__(
        self,
        api_key: str,
        timeout: float = 20.0,
        retries: int = 2,
        base_url: str = BASE_URL,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api_key = api_key
        self.retries = retries
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=base_url,
            headers={AUTH_HEADER: api_key},
            timeout=httpx.Timeout(timeout, connect=10.0),
            transport=transport,
        )

    @property
    def auth_headers(self) -> dict[str, str]:
        """Headers for a WebSocket handshake."""
        return {AUTH_HEADER: self.api_key}

    @staticmethod
    def ws_url(path: str, params: dict[str, Any]) -> str:
        """Full ``wss://`` URL with query parameters."""
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        return f"{WS_BASE_URL}{path}?{urlencode(clean)}"

    def _backoff(self, attempt: int, response: httpx.Response | None) -> float:
        retry_after = response.headers.get("retry-after") if response is not None else None
        if retry_after and retry_after.replace(".", "", 1).isdigit():
            return min(float(retry_after), 5.0)
        return 0.5 * (2**attempt)

    def _send(self, build: Callable[[], httpx.Request], stream: bool = False) -> httpx.Response:
        """Send a request, retrying 429/5xx. Raises :class:`SarvamError` on failure."""
        for attempt in range(self.retries + 1):
            try:
                response = self._http.send(build(), stream=stream)
            except httpx.HTTPError as exc:
                raise SarvamError(f"Network error: {exc}") from exc
            if response.status_code < 400:
                return response
            if stream:
                response.read()
                response.close()
            error = error_from_response(response)
            if not error.retryable or attempt == self.retries:
                raise error
            delay = self._backoff(attempt, response)
            logger.warning("Sarvam %s; retrying in %.1fs", error, delay)
            self._sleep(delay)
        raise AssertionError("unreachable")

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST JSON and return the decoded JSON response."""
        response = self._send(lambda: self._http.build_request("POST", path, json=payload))
        try:
            return response.json()
        except ValueError as exc:
            raise SarvamError(f"Invalid JSON from {path}", response.status_code) from exc

    def post_multipart(
        self, path: str, data: dict[str, str], files: dict[str, tuple[str, bytes, str]]
    ) -> dict[str, Any]:
        """POST multipart form data (used for audio uploads) and return JSON."""
        response = self._send(
            lambda: self._http.build_request("POST", path, data=data, files=files)
        )
        try:
            return response.json()
        except ValueError as exc:
            raise SarvamError(f"Invalid JSON from {path}", response.status_code) from exc

    @contextmanager
    def stream_post(self, path: str, payload: dict[str, Any]) -> Iterator[httpx.Response]:
        """POST JSON and yield the streaming response (caller iterates the bytes)."""
        response = self._send(
            lambda: self._http.build_request("POST", path, json=payload), stream=True
        )
        try:
            yield response
        finally:
            response.close()

    def close(self) -> None:
        """Close the connection pool."""
        self._http.close()

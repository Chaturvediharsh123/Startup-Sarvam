"""Chat Completions with tool calling (``POST /v1/chat/completions``).

Request and response shapes follow docs/sarvam_api_notes.md, section 3. Every request
has a hard deadline (``settings.llm_timeout_s``): the shared HTTP client has no
per-request timeout, so the call runs on a short-lived worker thread and a
:class:`SarvamError` is raised when the deadline passes.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from sarvam.client import SarvamClient, SarvamError

if TYPE_CHECKING:
    from core.config import Settings

logger = logging.getLogger(__name__)

CHAT_PATH = "/v1/chat/completions"
TIMEOUT_CODE = "llm_timeout"

T = TypeVar("T")


def call_with_timeout(func: Callable[[], T], timeout_s: float | None) -> T:
    """Run ``func`` with a deadline; raise :class:`SarvamError` if it is not done in time.

    The worker is a daemon thread: a request that hangs past the deadline is
    abandoned (its late answer is ignored) and never blocks shutdown.
    """
    if not timeout_s or timeout_s <= 0:
        return func()
    box: dict[str, Any] = {}
    done = threading.Event()

    def run() -> None:
        try:
            box["value"] = func()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            box["error"] = exc
        finally:
            done.set()

    threading.Thread(target=run, name="llm-request", daemon=True).start()
    if not done.wait(timeout_s):
        raise SarvamError(f"LLM did not answer within {timeout_s:g} s", code=TIMEOUT_CODE)
    if "error" in box:
        raise box["error"]
    return box["value"]


class ChatModel(Protocol):
    """Anything that can answer a chat request (the real LLM or a test fake)."""

    def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """Return the assistant message dict."""
        ...


class SarvamLLM:
    """Calls Sarvam chat completions and returns ``choices[0].message``.

    Args:
        client: shared :class:`SarvamClient` (handles auth, timeout, retries).
        model: ``sarvam-105b`` or ``sarvam-105b-conversations``.
        reasoning: ``low``/``medium``/``high``/``max``, or ``none`` to turn thinking off.
        max_tokens: completion budget; reasoning tokens count against it.
        temperature: low (0.2) so tool choice is stable.
        timeout_s: hard deadline per request; ``None`` or 0 waits for the HTTP client.
    """

    def __init__(
        self,
        client: SarvamClient,
        model: str,
        reasoning: str = "low",
        max_tokens: int = 1024,
        temperature: float = 0.2,
        timeout_s: float | None = None,
    ) -> None:
        self.client = client
        self.model = model
        self.reasoning = reasoning
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout_s = timeout_s

    @classmethod
    def from_settings(cls, client: SarvamClient, settings: Settings) -> SarvamLLM:
        """Build the LLM from :class:`core.config.Settings` (model, reasoning, timeout)."""
        return cls(
            client,
            settings.llm_model,
            reasoning=settings.llm_reasoning,
            timeout_s=settings.llm_timeout_s,
        )

    def build_payload(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> dict[str, Any]:
        """The JSON body for one chat request."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "reasoning_effort": None if self.reasoning == "none" else self.reasoning,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """Send one chat request and return the assistant message.

        Raises:
            SarvamError: on API failure, a malformed response, or when the request
                takes longer than :attr:`timeout_s` (``code == "llm_timeout"``).
        """
        payload = self.build_payload(messages, tools)
        body = call_with_timeout(lambda: self.client.post_json(CHAT_PATH, payload), self.timeout_s)
        try:
            choice = body["choices"][0]
            message = dict(choice["message"])
        except (KeyError, IndexError, TypeError) as exc:
            raise SarvamError(f"Unexpected chat response: {str(body)[:200]}") from exc
        if choice.get("finish_reason") == "length" and not message.get("content"):
            logger.warning("LLM hit max_tokens with no visible reply (reasoning used it up)")
        usage = body.get("usage") or {}
        logger.debug(
            "LLM finish=%s tokens=%s", choice.get("finish_reason"), usage.get("total_tokens")
        )
        message.pop("reasoning_content", None)
        return message

"""Chat Completions with tool calling (``POST /v1/chat/completions``).

Request and response shapes follow docs/sarvam_api_notes.md, section 3.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from sarvam.client import SarvamClient, SarvamError

logger = logging.getLogger(__name__)

CHAT_PATH = "/v1/chat/completions"


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
    """

    def __init__(
        self,
        client: SarvamClient,
        model: str,
        reasoning: str = "low",
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> None:
        self.client = client
        self.model = model
        self.reasoning = reasoning
        self.max_tokens = max_tokens
        self.temperature = temperature

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
            SarvamError: on API failure or a malformed response.
        """
        body = self.client.post_json(CHAT_PATH, self.build_payload(messages, tools))
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

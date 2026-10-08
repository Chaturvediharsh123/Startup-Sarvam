"""The brain: turns one user sentence into actions and a short spoken reply.

Flow for :meth:`Brain.handle`:

1. Detect the user's language and script.
2. Build messages: system prompt + memory context + recent turns + the new sentence.
3. Call the LLM with the action tools plus brain-only tools (``remember_fact``,
   ``clear_conversation``).
4. Run every tool call through :func:`safety.guard.execute`.
5. Informational results go back to the LLM for a second call, so the answer is
   phrased in the user's language. Otherwise the first reply text is used, or a
   canned confirmation.
6. If anything was denied, blocked or failed, the reply says so.
7. Save both turns to memory.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from actions.registry import tool_schemas
from brain.llm_client import ChatModel
from brain.memory import Memory
from core.config import Settings
from language.detect import LanguageInfo, detect_language
from language.phrases import phrase
from safety import guard as safety
from safety.guard import ActionResult, AskUser

logger = logging.getLogger(__name__)

MAX_TOOL_CALLS = 5

LANGUAGE_NAMES = {
    "hi": "Hindi", "en": "English", "bn": "Bengali", "ta": "Tamil", "te": "Telugu",
    "kn": "Kannada", "ml": "Malayalam", "mr": "Marathi", "gu": "Gujarati", "pa": "Punjabi",
    "od": "Odia", "ur": "Urdu", "as": "Assamese", "ne": "Nepali",
}

SYSTEM_PROMPT = """You are Sarvam Sahayak, a friendly voice assistant that controls a \
Windows PC for Indian users, many of them elderly or new to computers.

Rules:
1. Reply in the SAME language AND script the user used: Hindi in Devanagari, Hinglish \
(Hindi in Roman letters) in Roman letters, Tamil in Tamil script, English in English, and so on.
2. Your reply is spoken aloud. Use 1-2 short, simple sentences. No markdown, lists, emojis or URLs.
3. When the user wants something done on the PC, call a tool. Use only the given tools. \
Never say you did something unless you called the tool for it.
4. "isko", "usko", "ise", "it", "that" mean the last action shown in the context.
5. When the user tells you a personal fact (name, city, preferred language, family), save it \
with remember_fact. Use the known facts when they ask about themselves.
6. For a leave application, letter or note, write the COMPLETE text yourself in the user's \
language and pass it to save_note.
7. If the user asks you to forget everything ("sab bhool jao"), call clear_conversation.
8. If the request is unclear, ask one short question instead of guessing.
9. General questions and small talk need no tool. Just answer briefly."""

BRAIN_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "remember_fact",
            "description": "Save a personal fact about the user for later (name, city, ...).",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Short English key, e.g. name"},
                    "value": {"type": "string", "description": "The fact, as the user said it"},
                },
                "required": ["key", "value"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear_conversation",
            "description": "Forget this conversation when the user asks (facts are kept).",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]


@dataclass
class Reply:
    """What the assistant says back, plus what it did and how long it took."""

    text: str
    language: str
    romanised: bool = False
    actions: list[ActionResult] = field(default_factory=list)
    timings: dict[str, int] = field(default_factory=dict)


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _parse_arguments(raw: Any) -> Any:
    """Tool-call arguments arrive as a JSON string; return the decoded value."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw  # safety.execute rejects non-dict arguments as an error


class Brain:
    """Conversation logic around the LLM, the safety gate and memory."""

    def __init__(
        self,
        memory: Memory,
        llm: ChatModel,
        settings: Settings,
        execute: Callable[..., ActionResult] = safety.execute,
    ) -> None:
        self.memory = memory
        self.llm = llm
        self.settings = settings
        self._execute = execute
        self.tools = tool_schemas() + BRAIN_TOOLS

    # -- prompt ------------------------------------------------------------
    def build_messages(self, user_text: str, info: LanguageInfo) -> list[dict[str, Any]]:
        """System prompt, memory context, recent history and the new user turn."""
        name = LANGUAGE_NAMES.get(info.code.split("-")[0], info.code)
        script = ", written in Roman letters" if info.romanised else ""
        system = f"{SYSTEM_PROMPT}\n\nThe user is speaking {name} ({info.code}){script}."
        note = self.memory.context_note()
        if note:
            system += f"\n\nContext:\n{note}"
        history = self.memory.recent_messages(self.settings.history_turns * 2)
        return [{"role": "system", "content": system}, *history,
                {"role": "user", "content": user_text}]

    # -- tools -------------------------------------------------------------
    def _brain_tool(self, name: str, args: Any) -> ActionResult:
        """Handle tools that live in the brain rather than on the PC."""
        if name == "remember_fact":
            if not isinstance(args, dict) or not args.get("key") or not args.get("value"):
                return ActionResult(name, {"raw": repr(args)}, "error", "key and value needed")
            self.memory.remember(str(args["key"]), str(args["value"]))
            return ActionResult(name, args, "ok", "Saved")
        self.memory.clear_session()
        return ActionResult(name, {}, "ok", "Conversation cleared")

    def _run_tools(
        self, calls: list[dict[str, Any]], info: LanguageInfo, ask_user: AskUser | None
    ) -> list[tuple[dict[str, Any], ActionResult]]:
        """Run each tool call (max :data:`MAX_TOOL_CALLS`) and pair it with its outcome."""
        results = []
        for call in calls[:MAX_TOOL_CALLS]:
            fn = call.get("function") or {}
            name, args = str(fn.get("name", "")), _parse_arguments(fn.get("arguments"))
            if name in ("remember_fact", "clear_conversation"):
                outcome = self._brain_tool(name, args)
            else:
                outcome = self._execute(
                    name, args, self.memory, ask_user, language=info.code, romanised=info.romanised
                )
            logger.info("Tool %s -> %s", name, outcome.status)
            results.append((call, outcome))
        return results

    def _second_pass(
        self,
        messages: list[dict[str, Any]],
        first: dict[str, Any],
        results: list[tuple[dict[str, Any], ActionResult]],
    ) -> str:
        """Send tool results back so the LLM can phrase the answer."""
        follow = [*messages, {"role": "assistant", "content": first.get("content"),
                              "tool_calls": [call for call, _ in results]}]
        for call, outcome in results:
            payload = {"status": outcome.status, "result": outcome.result}
            follow.append({"role": "tool", "tool_call_id": call.get("id", ""),
                           "content": json.dumps(payload, ensure_ascii=False, default=str)})
        reply = self.llm.chat(follow, self.tools)
        return (reply.get("content") or "").strip()

    @staticmethod
    def _failure_text(outcomes: list[ActionResult], info: LanguageInfo) -> str:
        """Canned sentence(s) for every action that did not succeed."""
        keys: list[str] = []
        for outcome in outcomes:
            if outcome.ok:
                continue
            key = outcome.status if outcome.status != "blocked" else (
                "rate_limited" if outcome.extra.get("rate_limited") else "blocked")
            if key not in keys:
                keys.append(key)
        return " ".join(phrase(k, info.code, romanised=info.romanised) for k in keys)

    # -- main entry ----------------------------------------------------------
    def handle(
        self, user_text: str, language: str | None = None, ask_user: AskUser | None = None
    ) -> Reply:
        """Answer one user sentence. Never raises; LLM failures give a spoken apology."""
        start = time.perf_counter()
        info = detect_language(user_text, language, self.settings.default_language)
        messages = self.build_messages(user_text, info)
        timings: dict[str, int] = {}
        outcomes: list[ActionResult] = []
        try:
            text, outcomes = self._converse(messages, info, ask_user, timings)
        except Exception:  # noqa: BLE001 - the assistant must keep talking
            logger.exception("LLM turn failed")
            text = phrase("sorry_repeat", info.code, romanised=info.romanised)
            self.memory.add_message("user", user_text, info.code)
            timings["total_ms"] = _ms(start)
            return Reply(text, info.code, info.romanised, outcomes, timings)
        if not any(o.name == "clear_conversation" and o.ok for o in outcomes):
            self.memory.add_message("user", user_text, info.code)
            self.memory.add_message("assistant", text, info.code)
        timings["total_ms"] = _ms(start)
        return Reply(text, info.code, info.romanised, outcomes, timings)

    def _converse(
        self,
        messages: list[dict[str, Any]],
        info: LanguageInfo,
        ask_user: AskUser | None,
        timings: dict[str, int],
    ) -> tuple[str, list[ActionResult]]:
        """LLM call(s) plus tool execution. Returns the reply text and outcomes."""
        t = time.perf_counter()
        first = self.llm.chat(messages, self.tools)
        timings["llm_ms"] = _ms(t)
        calls = first.get("tool_calls") or []
        text = (first.get("content") or "").strip()
        if not calls:
            return text or phrase("sorry_repeat", info.code, romanised=info.romanised), []
        results = self._run_tools(calls, info, ask_user)
        outcomes = [o for _, o in results]
        timings["action_ms"] = sum(o.duration_ms for o in outcomes)
        if any(o.informational and o.ok for o in outcomes):
            t = time.perf_counter()
            text = self._second_pass(messages, first, results)
            timings["llm_ms"] += _ms(t)
        failures = self._failure_text(outcomes, info)
        if failures:
            succeeded = any(o.ok for o in outcomes)
            prefix = text if any(o.informational and o.ok for o in outcomes) else (
                phrase("done", info.code, romanised=info.romanised) if succeeded else "")
            return f"{prefix} {failures}".strip(), outcomes
        return text or phrase("done", info.code, romanised=info.romanised), outcomes

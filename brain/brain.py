"""The brain (WS3): one user sentence -> a short spoken reply and at most one tool call.

Flow for :meth:`SarvamBrain.decide` (one LLM call per turn):

1. Detect the user's language and script (STT code, else the UI language hint,
   else guess from the text).
2. Build messages: system prompt (with the memory context note) + the last
   ``history_turns`` turns + the new sentence.
3. Call the LLM once with the action tools (from the ActionRunner) plus the
   brain-only tools ``remember_fact`` and ``clear_conversation``.
4. Run brain-only tools here; keep only the FIRST PC tool call; log the rest.
5. Return a :class:`core.events.BrainResult`. The orchestrator sends the tool call
   to the safety guard and the ActionRunner; the brain never runs PC actions.

After the action, :meth:`SarvamBrain.summarise` phrases the outcome with fixed
templates in the user's language (no second LLM call), and :meth:`record_turn` /
:meth:`record_action` save the turn to memory.
"""

from __future__ import annotations

import itertools
import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any

from brain.llm_client import ChatModel
from brain.memory import ACTION_STATUSES, Memory
from brain.prompts import build_system_prompt, name_for_code
from brain.tools_schema import BRAIN_TOOL_NAMES, CLEAR_CONVERSATION, REMEMBER_FACT, build_tools
from core.config import Settings
from core.events import ActionResult, BrainResult, ToolCall
from core.interfaces import CancelToken, Health
from language.detect import LanguageInfo, detect_language
from language.phrases import phrase, phrase_key

logger = logging.getLogger(__name__)

MODULE = "brain"
MAX_RESULT_CHARS = 500
_NUMBER = re.compile(r"\d{1,4}")
_call_ids = itertools.count(1)


def _ms(start: float) -> int:
    """Milliseconds since ``start`` (a ``perf_counter`` value)."""
    return int((time.perf_counter() - start) * 1000)


def parse_arguments(raw: Any) -> dict[str, Any] | None:
    """Decode tool-call arguments (a JSON string or a dict). ``None`` if not a JSON object."""
    if isinstance(raw, dict):
        return raw
    if raw is None or raw == "":
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def app_label(name: Any) -> str:
    """Spoken form of an app key: ``file_manager`` -> ``File Manager``."""
    return str(name or "").replace("_", " ").strip().title()


def _first_number(*values: Any) -> int | None:
    """The first whole number in ``values`` (numbers, or text containing digits)."""
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            return int(value)
        match = _NUMBER.search(str(value or ""))
        if match:
            return int(match.group())
    return None


def _result_text(outcome: ActionResult) -> str:
    """Lower-case English text of an outcome (fact + string result), for keyword checks."""
    result = outcome.result if isinstance(outcome.result, str) else ""
    return f"{outcome.fact} {result}".lower()


# ---------------------------------------------------------------------------
# Result templates (used by SarvamBrain.summarise and FakeBrain)
# ---------------------------------------------------------------------------

Say = Callable[..., str]


def _informational_text(outcome: ActionResult, say: Say) -> str | None:
    """Fill the numbers of ``system_status`` / ``current_time`` into their templates."""
    data = outcome.result if isinstance(outcome.result, dict) else None
    if data is None:
        return None
    if outcome.name == "system_status" or "cpu_percent" in data:
        cpu, memory = data.get("cpu_percent", "?"), data.get("memory_percent", "?")
        battery = data.get("battery_percent")
        if battery is None:
            return say("status_no_battery", cpu=cpu, memory=memory)
        return say("status", battery=battery, cpu=cpu, memory=memory)
    if outcome.name == "current_time" or "time" in data:
        return say("time", time=data.get("time", ""), date=data.get("date", ""))
    return None


_SIMPLE_SUCCESS = {
    "type_text": "typed",
    "take_screenshot": "screenshot_saved",
    "lock_pc": "locked",
    "save_note": "note_saved",
}


def _success_text(outcome: ActionResult, say: Say, shutdown_delay_s: int) -> str | None:
    """Template for a successful action, or ``None`` when there is none for it."""
    info = _informational_text(outcome, say)
    if info is not None:
        return info
    args, text, name = outcome.args or {}, _result_text(outcome), outcome.name
    if name == "open_app":
        return say("opened", app=app_label(args.get("name")))
    if name == "close_app":
        key = "not_running" if "not running" in text else "closed"
        return say(key, app=app_label(args.get("name")))
    if name in ("set_volume", "change_volume"):
        direction = str(args.get("direction", ""))
        if direction in ("mute", "unmute") or "muted" in text:
            unmuted = direction == "unmute" or "unmuted" in text
            return say("volume_unmuted" if unmuted else "volume_muted")
        level = _first_number(outcome.fact, outcome.result, args.get("level"))
        return say("volume_set", level=level) if level is not None else None
    if name == "web_search":
        return say("searched", query=str(args.get("query", "")).strip())
    if name == "youtube_search":
        return say("youtube_searched", query=str(args.get("query", "")).strip())
    if name == "shutdown_pc":
        return say("shutdown_scheduled", delay=_first_number(text) or shutdown_delay_s)
    if name == "cancel_shutdown":
        return say("no_shutdown" if "no shutdown" in text else "shutdown_cancelled")
    return say(_SIMPLE_SUCCESS[name]) if name in _SIMPLE_SUCCESS else None


def _not_installed(outcome: ActionResult) -> bool:
    """True when an error outcome says the app is missing from this PC."""
    if outcome.extra.get("reason") == "not_installed" or outcome.extra.get("not_installed"):
        return True
    text = f"{outcome.fact} {outcome.result}".lower()
    return "not installed" in text or "not found" in text


def summarise_outcome(
    outcome: ActionResult,
    language: str | None,
    romanised: bool = False,
    shutdown_delay_s: int = 60,
) -> str:
    """One short sentence about an action outcome, in the user's language and script.

    Statuses: ``ok`` (per-action template; numbers filled in for informational
    actions), ``error`` (``not_installed`` when the app is missing), ``denied``,
    ``blocked`` (``rate_limited`` when ``extra["rate_limited"]``), ``timeout`` and
    ``cancelled``.
    """

    def say(key: str, **values: Any) -> str:
        return phrase(key, language, romanised=romanised, **values)

    status = outcome.status
    if status == "ok":
        text = _success_text(outcome, say, shutdown_delay_s)
        if text:
            return text
        if outcome.fact and phrase_key(language, romanised) == "en":
            fact = outcome.fact.strip().rstrip(".")
            return f"{fact[:1].upper()}{fact[1:]}."
        return say("done")
    if status == "error":
        app = (outcome.args or {}).get("name")
        if app and _not_installed(outcome):
            return say("not_installed", app=app_label(app))
        return say("error")
    if status == "blocked":
        return say("rate_limited" if outcome.extra.get("rate_limited") else "not_allowed")
    if status in ("denied", "timeout", "cancelled"):
        return say(status)
    return say("error")


# ---------------------------------------------------------------------------
# The brain
# ---------------------------------------------------------------------------


class SarvamBrain:
    """Implements :class:`core.interfaces.Brain` with one Sarvam LLM call per turn.

    Args:
        memory: conversation memory (owned and closed by ``main``).
        llm: the chat model (:class:`brain.llm_client.SarvamLLM` or a fake).
        settings: reads ``history_turns``, ``default_language``, ``language_hint``
            and ``shutdown_delay_s``.
        action_schemas: OpenAI-style action tools from ``ActionRunner.tool_schemas()``.
    """

    def __init__(
        self,
        memory: Memory,
        llm: ChatModel,
        settings: Settings,
        action_schemas: list[dict[str, Any]] | None = None,
    ) -> None:
        self.memory = memory
        self.llm = llm
        self.settings = settings
        self.tools = build_tools(action_schemas)
        self._running = False
        self._last_error = ""
        self._skip_record = False  # set when clear_conversation ran in this turn

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        """Mark the brain ready. It owns no threads or connections."""
        self._running = True

    def stop(self) -> None:
        """Mark the brain stopped. Memory is closed by its owner, not here."""
        self._running = False

    def health(self) -> Health:
        """Healthy unless the last LLM call failed."""
        if self._last_error:
            return Health(MODULE, False, f"LLM failed: {self._last_error}"[:200])
        return Health(MODULE, True, "ready" if self._running else "stopped")

    # -- prompt --------------------------------------------------------------
    def language_info(self, text: str, language: str | None) -> LanguageInfo:
        """Language and script of ``text``: STT code, else the UI hint, else a guess."""
        hint = language or self.settings.language_hint or None
        return detect_language(text, hint, self.settings.default_language)

    def build_messages(self, user_text: str, info: LanguageInfo) -> list[dict[str, Any]]:
        """System prompt (with memory context), the last turns, and the new sentence."""
        system = build_system_prompt(
            name_for_code(info.code), info.code, info.romanised, self.memory.context_note()
        )
        history = self.memory.recent_messages(self.settings.history_turns * 2)
        return [
            {"role": "system", "content": system},
            *history,
            {"role": "user", "content": user_text},
        ]

    # -- decide --------------------------------------------------------------
    def decide(
        self, text: str, language: str | None, cancel: CancelToken | None = None
    ) -> BrainResult:
        """One LLM call -> spoken reply + at most one tool call. Never raises."""
        self._skip_record = False
        try:
            info = self.language_info(text, language)
        except Exception:  # noqa: BLE001 - detection must never stop a turn
            logger.exception("Language detection failed")
            info = LanguageInfo(self.settings.default_language, "unknown", False)

        def apology(llm_ms: int = 0) -> BrainResult:
            reply = phrase("sorry_repeat", info.code, romanised=info.romanised)
            return BrainResult(reply, info.code, info.romanised, None, llm_ms)

        if cancel is not None and cancel.cancelled:
            reply = phrase("cancelled", info.code, romanised=info.romanised)
            return BrainResult(reply, info.code, info.romanised)
        start = time.perf_counter()
        try:
            message = self.llm.chat(self.build_messages(text, info), self.tools)
        except Exception as exc:  # noqa: BLE001 - failures become a spoken apology
            self._last_error = str(exc) or type(exc).__name__
            logger.warning("LLM call failed after %d ms: %s", _ms(start), exc)
            return apology(_ms(start))
        llm_ms = _ms(start)
        self._last_error = ""
        try:
            return self._interpret(message, info, llm_ms)
        except Exception:  # noqa: BLE001 - a malformed answer must not crash the turn
            logger.exception("Could not use the LLM answer: %r", message)
            return apology(llm_ms)

    def _interpret(self, message: dict[str, Any], info: LanguageInfo, llm_ms: int) -> BrainResult:
        """Split the LLM message into reply text, brain-only tools and one PC tool call."""
        content = message.get("content")
        reply = content.strip() if isinstance(content, str) else ""
        tool_call: ToolCall | None = None
        brain_phrases: list[str] = []
        dropped: list[str] = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") or {}
            name = str(function.get("name", "")).strip()
            args = parse_arguments(function.get("arguments"))
            if not name or args is None:
                logger.warning("Ignoring tool call with bad arguments: %r", raw)
                dropped.append(name or "?")
                continue
            if name in BRAIN_TOOL_NAMES:
                key = self._run_brain_tool(name, args)
                if key:
                    brain_phrases.append(key)
                continue
            if tool_call is None:
                call_id = str(raw.get("id") or f"brain_call_{next(_call_ids)}")
                tool_call = ToolCall(name, dict(args), call_id)
            else:
                dropped.append(name)
        if dropped:
            logger.info("One action per turn: dropped extra tool calls %s", dropped)
        if not reply:
            key = "working" if tool_call else (brain_phrases[-1] if brain_phrases else None)
            reply = phrase(key or "sorry_repeat", info.code, romanised=info.romanised)
        return BrainResult(reply, info.code, info.romanised, tool_call, llm_ms)

    def _run_brain_tool(self, name: str, args: dict[str, Any]) -> str | None:
        """Run ``remember_fact`` / ``clear_conversation``. Returns a phrase key, or None."""
        try:
            if name == REMEMBER_FACT:
                key, value = str(args.get("key", "")).strip(), str(args.get("value", "")).strip()
                if not key or not value:
                    logger.warning("remember_fact needs key and value, got %r", args)
                    return None
                self.memory.remember(key, value)
                return "remembered"
            if name == CLEAR_CONVERSATION:
                self.memory.clear_session()
                self._skip_record = True
                return "forgot"
        except Exception:  # noqa: BLE001 - memory trouble must not break the turn
            logger.exception("Brain tool %s failed", name)
        return None

    # -- after the action ----------------------------------------------------
    def summarise(self, result: BrainResult, outcome: ActionResult) -> str:
        """The spoken result sentence for ``outcome``, in the language of ``result``."""
        try:
            return summarise_outcome(
                outcome, result.language, result.romanised, self.settings.shutdown_delay_s
            )
        except Exception:  # noqa: BLE001 - always say something
            logger.exception("Could not phrase the result of %s", outcome.name)
            key = "done" if outcome.ok else "error"
            return phrase(key, result.language, romanised=result.romanised)

    def record_turn(self, user_text: str, reply: str, language: str) -> None:
        """Save the user sentence and the spoken reply (skipped right after "forget all")."""
        if self._skip_record:
            self._skip_record = False
            return
        try:
            self.memory.add_message("user", user_text, language)
            if reply.strip():
                self.memory.add_message("assistant", reply, language)
        except Exception:  # noqa: BLE001 - memory trouble must not break the turn
            logger.exception("Could not save the turn to memory")

    def record_action(self, outcome: ActionResult) -> None:
        """Log one action outcome; the last ``ok`` one is what "isko" refers to next turn."""
        if outcome.name in BRAIN_TOOL_NAMES:
            return
        status = outcome.status if outcome.status in ACTION_STATUSES else "error"
        if outcome.result is None or isinstance(outcome.result, str):
            detail = outcome.result or ""
        else:
            detail = json.dumps(outcome.result, ensure_ascii=False, default=str)
        text = (outcome.fact or detail)[:MAX_RESULT_CHARS]
        try:
            self.memory.log_action(
                outcome.name, dict(outcome.args or {}), text, status, outcome.duration_ms
            )
        except Exception:  # noqa: BLE001 - memory trouble must not break the turn
            logger.exception("Could not log action %s", outcome.name)

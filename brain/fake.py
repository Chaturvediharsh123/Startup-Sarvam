"""Offline fakes for the brain, used by ``python main.py --mock`` and by tests.

* :class:`FakeLLM` maps simple keyword rules to the same assistant-message shape the
  real Chat Completions API returns (``content`` + ``tool_calls`` with JSON string
  ``arguments``). App names, closable apps and shortcuts come from the ``tools``
  passed to :meth:`FakeLLM.chat`, so it never imports ``actions``. An empty or
  missing ``tools`` list means "no restriction" (built-in defaults are used).
* :class:`FakeBrain` implements the :class:`core.interfaces.Brain` Protocol with
  scripted :class:`core.events.BrainResult` values, for orchestrator tests.

Nothing in this module talks to the network.
"""

from __future__ import annotations

import itertools
import json
import logging
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from typing import Any

from brain.brain import summarise_outcome
from brain.memory import Memory
from brain.tools_schema import enum_values, tool_names
from core.events import ActionResult, BrainResult
from core.interfaces import CancelToken, Health

logger = logging.getLogger(__name__)

UNKNOWN_REPLY = "Mock mode: samajh nahi aaya"

# Spoken/typed app names -> app keys used by the open_app / close_app tools.
APP_WORDS: dict[str, tuple[str, ...]] = {
    "notepad": ("notepad", "नोटपैड"),
    "calculator": ("calculator", "calc", "कैलकुलेटर"),
    "paint": ("paint", "पेंट"),
    "file_manager": ("file manager", "explorer", "files", "फाइल"),
    "chrome": ("chrome", "क्रोम"),
    "edge": ("edge",),
    "word": ("word", "वर्ड"),
    "excel": ("excel", "एक्सेल"),
    "powerpoint": ("powerpoint", "ppt"),
    "settings": ("settings", "सेटिंग"),
    "camera": ("camera", "कैमरा"),
}
# Used when no close_app tool is offered. File manager is never closed (it is the taskbar).
DEFAULT_CLOSABLE: tuple[str, ...] = tuple(app for app in APP_WORDS if app != "file_manager")

SHORTCUT_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("select_all", ("select all", "sab select")),
    ("new_tab", ("new tab", "naya tab")),
    ("close_tab", ("close tab", "tab band")),
    ("save", ("save",)),
    ("copy", ("copy",)),
    ("paste", ("paste",)),
    ("undo", ("undo",)),
    ("redo", ("redo",)),
    ("refresh", ("refresh", "reload")),
    ("minimize_all", ("minimize", "minimise", "desktop dikhao")),
    ("switch_window", ("switch window", "window badlo")),
    ("zoom_in", ("zoom in", "bada karo")),
    ("zoom_out", ("zoom out", "chhota karo")),
    ("enter", ("enter dabao", "press enter")),
)

CLOSE_WORDS = ("band karo", "band kar do", "band kardo", "close", "बंद")
OPEN_WORDS = ("kholo", "khol do", "open", "chalao", "start", "खोलो")
REFERENCE_WORDS = ("isko", "usko", "ise", "use band", "इसको", "इसे", "उसको", " it", "that")

SAMPLE_LEAVE_LETTER = """सेवा में,
प्रबंधक महोदय,

विषय: अवकाश हेतु प्रार्थना पत्र

महोदय,
निवेदन है कि मुझे पारिवारिक कारणों से दो दिन का अवकाश चाहिए। कृपया मुझे दिनांक ____ से ____ तक \
अवकाश प्रदान करने की कृपा करें।

धन्यवाद।

भवदीय,
____
(Mock mode sample letter)"""

Rule = Callable[[str, str], dict[str, Any] | None]


def _has(text: str, *words: str) -> bool:
    return any(w in text for w in words)


class FakeLLM:
    """Keyword-rule LLM that returns real-shaped assistant messages.

    Replies are phrased as "in progress" ("Chrome khol raha hoon"), never "done",
    matching the brain's rule that results are announced only after the action.

    Args:
        memory: used for "isko band karo" (last action) and "mera naam kya hai" (facts).
    """

    def __init__(self, memory: Memory) -> None:
        self.memory = memory
        self._ids = itertools.count(1)
        self._offered: set[str] | None = None
        self._apps: tuple[str, ...] = tuple(APP_WORDS)
        self._closable: tuple[str, ...] = DEFAULT_CLOSABLE
        self._shortcuts: tuple[str, ...] = tuple(name for name, _ in SHORTCUT_WORDS)
        self.rules: list[Rule] = [
            self._rule_name, self._rule_forget, self._rule_cancel_shutdown, self._rule_shutdown,
            self._rule_lock, self._rule_close_reference, self._rule_close_app, self._rule_letter,
            self._rule_type, self._rule_youtube, self._rule_search, self._rule_volume,
            self._rule_screenshot, self._rule_status, self._rule_time, self._rule_scroll,
            self._rule_open_app, self._rule_shortcut,
        ]

    # -- tools -------------------------------------------------------------------
    def _use_tools(self, tools: Iterable[dict[str, Any]] | None) -> None:
        """Read the offered tool names and enum values (apps, shortcuts) from ``tools``."""
        tools = list(tools or [])
        offered = tool_names(tools)
        self._offered = set(offered) if offered else None
        self._apps = enum_values(tools, "open_app", "name") or tuple(APP_WORDS)
        self._closable = enum_values(tools, "close_app", "name") or DEFAULT_CLOSABLE
        self._shortcuts = enum_values(tools, "press_shortcut", "shortcut") or tuple(
            name for name, _ in SHORTCUT_WORDS
        )

    def _find_app(self, text: str) -> str | None:
        """The first known app named in ``text`` (built-in words plus the tools' app keys)."""
        candidates = dict.fromkeys([*APP_WORDS, *self._apps, *self._closable])
        for app in candidates:
            words = (*APP_WORDS.get(app, ()), app.replace("_", " "))
            if any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", text) for w in words):
                return app
        return None

    # -- message shapes --------------------------------------------------------
    def _call(self, tool: str, content: str | None = None, /, **args: Any) -> dict[str, Any]:
        """Assistant message with one tool call, exactly like the real API."""
        return {
            "role": "assistant",
            "content": content,
            "tool_calls": [{
                "id": f"mock_call_{next(self._ids)}",
                "type": "function",
                "function": {"name": tool, "arguments": json.dumps(args, ensure_ascii=False)},
            }],
        }

    @staticmethod
    def _say(content: str) -> dict[str, Any]:
        return {"role": "assistant", "content": content}

    # -- entry point -------------------------------------------------------------
    def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """Answer like ``SarvamLLM.chat``: pick at most one tool by keyword rules."""
        if messages and messages[-1].get("role") == "tool":
            return self._say(self._summarise_tools(messages))
        self._use_tools(tools)
        user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
        original = str(user.get("content", "")) if user else ""
        text = " ".join(original.lower().split())
        for rule in self.rules:
            reply = rule(text, original)
            if reply is None:
                continue
            calls = reply.get("tool_calls") or []
            if self._offered is not None and any(
                c["function"]["name"] not in self._offered for c in calls
            ):
                return self._say(UNKNOWN_REPLY)
            return reply
        return self._say(UNKNOWN_REPLY)

    def _summarise_tools(self, messages: list[dict[str, Any]]) -> str:
        """Legacy second call: turn informational tool results into a short Hinglish answer."""
        names: dict[str, str] = {}
        for message in messages:
            for call in message.get("tool_calls") or []:
                names[call.get("id", "")] = call.get("function", {}).get("name", "")
        parts: list[str] = []
        for message in messages:
            if message.get("role") != "tool":
                continue
            payload = json.loads(message.get("content") or "{}")
            result = payload.get("result")
            name = names.get(message.get("tool_call_id", ""), "")
            if payload.get("status") != "ok" or not isinstance(result, dict):
                continue
            if name == "system_status":
                charging = "charge ho rahi hai" if result.get("charging") else "charge nahi ho rahi"
                battery = result.get("battery_percent")
                parts.append(
                    f"Battery {battery} percent hai aur {charging}." if battery is not None
                    else "Is PC mein battery nahi hai."
                )
                cpu, ram = result.get("cpu_percent"), result.get("memory_percent")
                parts.append(f"CPU {cpu}%, RAM {ram}%.")
            elif name == "current_time":
                parts.append(f"Abhi {result.get('time')} baje hain, {result.get('date')}.")
        return " ".join(parts) or "Mock mode: ho gaya."

    # -- rules (order matters; first match wins) -----------------------------------
    def _rule_name(self, text: str, original: str) -> dict[str, Any] | None:
        if re.search(r"(mera naam|my name is|मेरा नाम)\s+(kya|क्या|what)", text) or _has(
            text, "what is my name", "naam kya hai"
        ):
            name = self.memory.recall("name")
            return self._say(f"Aapka naam {name} hai." if name else "Mujhe aapka naam nahi pata.")
        match = re.search(r"(?:mera naam|मेरा नाम|my name is)\s+(.+?)(?:\s+(?:hai|है))?[.!।]?$",
                          original, re.IGNORECASE)
        if match:
            name = match.group(1).strip()
            return self._call("remember_fact", f"Theek hai, {name}.", key="name", value=name)
        return None

    def _rule_forget(self, text: str, original: str) -> dict[str, Any] | None:
        if _has(text, "sab bhool jao", "bhool jao", "forget everything", "सब भूल जाओ"):
            return self._call("clear_conversation", "Theek hai, sab bhool gaya.")
        return None

    def _rule_cancel_shutdown(self, text: str, original: str) -> dict[str, Any] | None:
        if _has(text, "shutdown") and _has(text, "cancel", "roko", "mat", "रोको"):
            return self._call("cancel_shutdown", "Shutdown rok raha hoon.")
        return None

    def _rule_shutdown(self, text: str, original: str) -> dict[str, Any] | None:
        pc = _has(text, "pc", "computer", "laptop", "कंप्यूटर", "system")
        if _has(text, "shutdown", "shut down") or (pc and _has(text, *CLOSE_WORDS)):
            return self._call("shutdown_pc", "PC band kar raha hoon.")
        return None

    def _rule_lock(self, text: str, original: str) -> dict[str, Any] | None:
        if _has(text, "lock", "लॉक"):
            return self._call("lock_pc", "Computer lock kar raha hoon.")
        return None

    def _rule_close_reference(self, text: str, original: str) -> dict[str, Any] | None:
        if not (_has(f" {text}", *REFERENCE_WORDS) and _has(text, *CLOSE_WORDS)):
            return None
        last = self.memory.last_action()
        app = last["args"].get("name") if last and last["name"] == "open_app" else None
        if app in self._closable:
            return self._call("close_app", f"{app} band kar raha hoon.", name=app)
        return self._say("Mock mode: pata nahi kise band karna hai. App ka naam boliye.")

    def _rule_close_app(self, text: str, original: str) -> dict[str, Any] | None:
        app = self._find_app(text)
        if app and _has(text, *CLOSE_WORDS):
            if app not in self._closable:
                return self._say(f"Mock mode: {app} band nahi kar sakta.")
            return self._call("close_app", f"{app} band kar raha hoon.", name=app)
        return None

    def _rule_letter(self, text: str, original: str) -> dict[str, Any] | None:
        if _has(text, "application", "letter", "chitthi", "patra", "अर्ज़ी", "एप्लीकेशन") and _has(
            text, "likho", "likh do", "write", "लिखो"
        ):
            return self._call("save_note", "Chhutti ki application likh raha hoon.",
                              title="Chhutti ki application", content=SAMPLE_LEAVE_LETTER)
        if re.search(r"\bnote\b", text) and _has(text, "likho", "write", "लिखो"):
            body = re.sub(r"(?i).*?(note|नोट)\s*(likho|write|लिखो)?\s*(ki|that|:)?\s*", "",
                          original, count=1).strip() or original
            return self._call("save_note", "Note save kar raha hoon.", title="Note", content=body)
        return None

    def _rule_type(self, text: str, original: str) -> dict[str, Any] | None:
        match = re.match(r"^\s*(?:likho|type|लिखो)\s*[:：-]\s*(.+)$", original, re.IGNORECASE)
        if match:
            return self._call("type_text", "Likh raha hoon.", text=match.group(1).strip())
        return None

    def _rule_youtube(self, text: str, original: str) -> dict[str, Any] | None:
        if "youtube" in text or "यूट्यूब" in text:
            query = re.sub(r"(?i)youtube|यूट्यूब|\b(?:pe|par|on|lagao|chalao|search|karo|"
                           r"dikhao|bajao|play)\b", " ", original)
            query = " ".join(query.split()) or "bhajan"
            return self._call("youtube_search", f"YouTube pe {query} dhoondh raha hoon.",
                              query=query)
        return None

    def _rule_search(self, text: str, original: str) -> dict[str, Any] | None:
        if _has(text, "google", "search", "dhoondo", "khojo"):
            query = re.sub(r"(?i)\b(?:google|pe|par|search|karo|dhoondo|khojo|for)\b", " ",
                           original)
            query = " ".join(query.split()) or original
            return self._call("web_search", f"{query} search kar raha hoon.", query=query)
        return None

    def _rule_volume(self, text: str, original: str) -> dict[str, Any] | None:
        if not _has(text, "volume", "awaaz", "aawaz", "आवाज़", "वॉल्यूम", "mute", "sound"):
            return None
        number = re.search(r"\d{1,3}", text)
        if number:
            level = max(0, min(100, int(number.group())))
            return self._call("set_volume", f"Volume {level} kar raha hoon.", level=level)
        if "unmute" in text:
            return self._call("change_volume", direction="unmute")
        if "mute" in text or _has(text, "band karo"):
            return self._call("change_volume", direction="mute")
        if _has(text, "kam", "ghatao", "down", "kum", "धीमी", "कम"):
            return self._call("change_volume", direction="down", step=10)
        return self._call("change_volume", direction="up", step=10)

    def _rule_screenshot(self, text: str, original: str) -> dict[str, Any] | None:
        if _has(text, "screenshot", "स्क्रीनशॉट", "screen shot"):
            return self._call("take_screenshot", "Screenshot le raha hoon.")
        return None

    def _rule_status(self, text: str, original: str) -> dict[str, Any] | None:
        if _has(text, "battery", "बैटरी", "system status", "cpu", "ram", "memory kitni"):
            return self._call("system_status")
        return None

    def _rule_time(self, text: str, original: str) -> dict[str, Any] | None:
        if _has(text, "baje", "time", "samay", "tareekh", "date", "समय", "बजे", "तारीख"):
            return self._call("current_time")
        return None

    def _rule_scroll(self, text: str, original: str) -> dict[str, Any] | None:
        if not _has(text, "scroll", "स्क्रॉल"):
            return None
        direction = "up" if _has(text, "upar", "up", "ऊपर") else "down"
        amount = "small" if _has(text, "thoda", "little") else (
            "large" if _has(text, "bahut", "zyada", "a lot") else "medium")
        return self._call("scroll", direction=direction, amount=amount)

    def _rule_open_app(self, text: str, original: str) -> dict[str, Any] | None:
        app = self._find_app(text)
        if app and app in self._apps and (_has(text, *OPEN_WORDS) or len(text.split()) <= 2):
            return self._call("open_app", f"{app} khol raha hoon.", name=app)
        return None

    def _rule_shortcut(self, text: str, original: str) -> dict[str, Any] | None:
        for shortcut, words in SHORTCUT_WORDS:
            if shortcut in self._shortcuts and _has(text, *words):
                return self._call("press_shortcut", shortcut=shortcut)
        return None


MockLLM = FakeLLM

Script = Mapping[str, BrainResult] | Callable[[str, str | None], BrainResult | None]


class FakeBrain:
    """A scripted :class:`core.interfaces.Brain` for orchestrator tests.

    Args:
        script: ``{text: BrainResult}`` or a callable ``(text, language) -> BrainResult``.
            Unknown text gets :data:`UNKNOWN_REPLY` with no tool call.
        summaries: optional ``{action name: sentence}``; otherwise the real templates.
        default_language: language of the fallback reply when none is given.
    """

    def __init__(
        self,
        script: Script | None = None,
        summaries: Mapping[str, str] | None = None,
        default_language: str = "hi-IN",
    ) -> None:
        self.script = script or {}
        self.summaries = dict(summaries or {})
        self.default_language = default_language
        self.decisions: list[tuple[str, str | None]] = []
        self.turns: list[tuple[str, str, str]] = []
        self.actions: list[ActionResult] = []
        self.running = False

    def start(self) -> None:
        """Mark the fake started."""
        self.running = True

    def stop(self) -> None:
        """Mark the fake stopped."""
        self.running = False

    def health(self) -> Health:
        """Always healthy."""
        return Health("brain", True, "fake")

    def decide(
        self, text: str, language: str | None, cancel: CancelToken | None = None
    ) -> BrainResult:
        """Return the scripted result for ``text`` (a fresh copy each call)."""
        self.decisions.append((text, language))
        if callable(self.script):
            result = self.script(text, language)
        else:
            result = self.script.get(text) or self.script.get(text.strip().lower())
        if result is None:
            return BrainResult(UNKNOWN_REPLY, language or self.default_language)
        return replace(result)

    def summarise(self, result: BrainResult, outcome: ActionResult) -> str:
        """Scripted summary for the action, else the real per-action template."""
        if outcome.name in self.summaries:
            return self.summaries[outcome.name]
        return summarise_outcome(outcome, result.language, result.romanised)

    def record_turn(self, user_text: str, reply: str, language: str) -> None:
        """Remember the turn for assertions."""
        self.turns.append((user_text, reply, language))

    def record_action(self, outcome: ActionResult) -> None:
        """Remember the action outcome for assertions."""
        self.actions.append(outcome)

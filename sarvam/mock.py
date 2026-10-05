"""Offline stand-ins for Sarvam, used by ``python main.py --mock``.

* :class:`MockLLM` maps simple keyword rules to the same assistant-message shape
  the real Chat Completions API returns (``content`` + ``tool_calls`` with JSON
  string ``arguments``), so the brain, safety gate and actions run unchanged.
* :class:`MockTTS` prints the reply and, if ``pyttsx3`` is installed, speaks it
  with the offline Windows voice.

Nothing in this module talks to the network.
"""

from __future__ import annotations

import itertools
import json
import logging
import re
from collections.abc import Callable, Iterator
from typing import Any

from assistant.actions import PROCESS_NAMES
from assistant.memory import Memory

logger = logging.getLogger(__name__)

UNKNOWN_REPLY = "Mock mode: samajh nahi aaya"

# Spoken/typed app names -> keys of assistant.actions.APPS
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


def _find_app(text: str) -> str | None:
    for app, words in APP_WORDS.items():
        if any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", text) for w in words):
            return app
    return None


def _has(text: str, *words: str) -> bool:
    return any(w in text for w in words)


class MockLLM:
    """Keyword-rule LLM that returns real-shaped assistant messages.

    Args:
        memory: used for "isko band karo" (last action) and "mera naam kya hai" (facts).
    """

    def __init__(self, memory: Memory) -> None:
        self.memory = memory
        self._ids = itertools.count(1)
        self.rules: list[Rule] = [
            self._rule_name, self._rule_forget, self._rule_cancel_shutdown, self._rule_shutdown,
            self._rule_lock, self._rule_close_reference, self._rule_close_app, self._rule_letter,
            self._rule_type, self._rule_youtube, self._rule_search, self._rule_volume,
            self._rule_screenshot, self._rule_status, self._rule_time, self._rule_scroll,
            self._rule_open_app, self._rule_shortcut,
        ]

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
        """Answer like ``SarvamLLM.chat``: first call picks a tool, second phrases results."""
        if messages and messages[-1].get("role") == "tool":
            return self._say(self._summarise_tools(messages))
        user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
        original = str(user.get("content", "")) if user else ""
        text = " ".join(original.lower().split())
        for rule in self.rules:
            reply = rule(text, original)
            if reply is not None:
                return reply
        return self._say(UNKNOWN_REPLY)

    def _summarise_tools(self, messages: list[dict[str, Any]]) -> str:
        """Second call: turn informational tool results into a short Hinglish answer."""
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
            return self._call("cancel_shutdown")
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
        if app in PROCESS_NAMES:
            return self._call("close_app", f"{app} band kar raha hoon.", name=app)
        return self._say("Mock mode: pata nahi kise band karna hai. App ka naam boliye.")

    def _rule_close_app(self, text: str, original: str) -> dict[str, Any] | None:
        app = _find_app(text)
        if app and _has(text, *CLOSE_WORDS):
            if app not in PROCESS_NAMES:
                return self._say(f"Mock mode: {app} band nahi kar sakta.")
            return self._call("close_app", f"{app} band kar raha hoon.", name=app)
        return None

    def _rule_letter(self, text: str, original: str) -> dict[str, Any] | None:
        if _has(text, "application", "letter", "chitthi", "patra", "अर्ज़ी", "एप्लीकेशन") and _has(
            text, "likho", "likh do", "write", "लिखो"
        ):
            return self._call("save_note", "Chhutti ki application likh di.",
                              title="Chhutti ki application", content=SAMPLE_LEAVE_LETTER)
        if re.search(r"\bnote\b", text) and _has(text, "likho", "write", "लिखो"):
            body = re.sub(r"(?i).*?(note|नोट)\s*(likho|write|लिखो)?\s*(ki|that|:)?\s*", "",
                          original, count=1).strip() or original
            return self._call("save_note", "Note save kar diya.", title="Note", content=body)
        return None

    def _rule_type(self, text: str, original: str) -> dict[str, Any] | None:
        match = re.match(r"^\s*(?:likho|type|लिखो)\s*[:：-]\s*(.+)$", original, re.IGNORECASE)
        if match:
            return self._call("type_text", "Likh diya.", text=match.group(1).strip())
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
            return self._call("set_volume", f"Volume {level} kar diya.", level=level)
        if "unmute" in text:
            return self._call("change_volume", direction="unmute")
        if "mute" in text or _has(text, "band karo"):
            return self._call("change_volume", direction="mute")
        if _has(text, "kam", "ghatao", "down", "kum", "धीमी", "कम"):
            return self._call("change_volume", direction="down", step=10)
        return self._call("change_volume", direction="up", step=10)

    def _rule_screenshot(self, text: str, original: str) -> dict[str, Any] | None:
        if _has(text, "screenshot", "स्क्रीनशॉट", "screen shot"):
            return self._call("take_screenshot", "Screenshot le liya.")
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
        app = _find_app(text)
        if app and (_has(text, *OPEN_WORDS) or len(text.split()) <= 2):
            return self._call("open_app", f"{app} khol raha hoon.", name=app)
        return None

    def _rule_shortcut(self, text: str, original: str) -> dict[str, Any] | None:
        for shortcut, words in SHORTCUT_WORDS:
            if _has(text, *words):
                return self._call("press_shortcut", shortcut=shortcut)
        return None


class MockTTS:
    """Prints the reply and, when ``pyttsx3`` is installed, speaks it offline.

    It yields no PCM, so :class:`audio.player.AudioPlayer` never opens a sound
    device, but the mic mute flag is still set while speaking.
    """

    def __init__(self, settings: Any = None, output: Callable[[str], None] = print) -> None:
        self.settings = settings
        self._output = output
        self._warned = False

    def speak(self, text: str) -> None:
        """Print ``text`` and say it with the Windows offline voice if available."""
        self._output(f"[voice] {text}")
        try:
            import pyttsx3
        except ImportError:
            if not self._warned:
                logger.info("pyttsx3 not installed; mock voice is text-only")
                self._warned = True
            return
        try:
            engine = pyttsx3.init()
            engine.say(text)
            engine.runAndWait()
            engine.stop()
        except Exception:  # noqa: BLE001 - offline voice is optional
            logger.warning("Offline voice failed", exc_info=True)

    def stream(self, text: str, language: str | None) -> Iterator[bytes]:
        """Same interface as ``SarvamTTS.stream``; speaks, then yields nothing."""
        self.speak(text)
        yield from ()

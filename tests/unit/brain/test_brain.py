"""Tests for SarvamBrain with a small scripted chat model (no network, no actions)."""

from __future__ import annotations

import ast
import json
import logging
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from brain.brain import SarvamBrain, parse_arguments, summarise_outcome
from brain.llm_client import SarvamLLM
from brain.memory import Memory
from core.config import Settings
from core.events import ActionResult, BrainResult
from core.interfaces import CancelToken
from sarvam.client import SarvamError

BRAIN_DIR = Path(__file__).resolve().parents[3] / "brain"


def schema(tool: str, /, **props: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool,
            "description": tool,
            "parameters": {"type": "object", "properties": props, "required": list(props)},
        },
    }


ACTION_SCHEMAS = [
    schema("open_app", name={"type": "string", "enum": ["chrome", "notepad"]}),
    schema("close_app", name={"type": "string", "enum": ["chrome", "notepad"]}),
    schema("set_volume", level={"type": "integer"}),
    schema("system_status"),
    schema("current_time"),
]


class StubLLM:
    """Returns scripted assistant messages (or raises) and records every request."""

    def __init__(self, *responses: dict[str, Any] | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []
        self.tools: list[Any] = []
        self.on_call: Any = None

    def chat(self, messages: list[dict[str, Any]], tools: Any = None) -> dict[str, Any]:
        self.calls.append(messages)
        self.tools.append(tools)
        if self.on_call:
            self.on_call()
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def tool_call(name: str, args: dict[str, Any] | str, call_id: str = "call_1") -> dict[str, Any]:
    arguments = args if isinstance(args, str) else json.dumps(args)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def msg(content: str | None = None, *calls: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        out["tool_calls"] = list(calls)
    return out


def outcome(name: str, status: str = "ok", result: Any = "", **kw: Any) -> ActionResult:
    return ActionResult(name, kw.pop("args", {}), status, result, **kw)


@pytest.fixture
def mem() -> Iterator[Memory]:
    with Memory(":memory:") as m:
        yield m


def make(mem: Memory, settings: Settings, llm: Any) -> SarvamBrain:
    return SarvamBrain(mem, llm, settings, ACTION_SCHEMAS)


# -- architecture rules -------------------------------------------------------------


def test_brain_never_imports_actions_or_safety() -> None:
    for path in BRAIN_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                assert not name.startswith(("actions", "safety")), f"{path.name}: {name}"


def test_implements_brain_protocol(mem: Memory, settings: Settings) -> None:
    brain = make(mem, settings, StubLLM())
    for method in ("decide", "summarise", "record_turn", "start", "stop", "health"):
        assert callable(getattr(brain, method))


# -- decide ---------------------------------------------------------------------------


def test_plain_answer_no_tools(mem: Memory, settings: Settings) -> None:
    llm = StubLLM(msg("भारत की राजधानी नई दिल्ली है।"))
    result = make(mem, settings, llm).decide("भारत की राजधानी क्या है?", None)
    assert isinstance(result, BrainResult)
    assert result.reply == "भारत की राजधानी नई दिल्ली है।"
    assert result.language == "hi-IN" and result.romanised is False
    assert result.tool_call is None
    assert result.llm_ms >= 0
    assert len(llm.calls) == 1


def test_tools_and_system_prompt_sent(mem: Memory, settings: Settings) -> None:
    llm = StubLLM(msg("ok"))
    make(mem, settings, llm).decide("Chrome kholo", None)
    names = [t["function"]["name"] for t in llm.tools[0]]
    assert names == [
        "open_app", "close_app", "set_volume", "system_status", "current_time",
        "remember_fact", "clear_conversation",
    ]
    system = llm.calls[0][0]
    assert system["role"] == "system"
    assert "Roman letters" in system["content"] and "(hi-IN)" in system["content"]
    assert llm.calls[0][-1] == {"role": "user", "content": "Chrome kholo"}


def test_one_tool_per_turn(
    mem: Memory, settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    llm = StubLLM(msg(
        "Chrome khol raha hoon.",
        tool_call("open_app", {"name": "chrome"}, "c1"),
        tool_call("set_volume", {"level": 30}, "c2"),
        tool_call("close_app", {"name": "notepad"}, "c3"),
    ))
    with caplog.at_level(logging.INFO, logger="brain.brain"):
        result = make(mem, settings, llm).decide("Chrome kholo aur volume 30", None)
    assert result.tool_call is not None
    assert result.tool_call.name == "open_app"
    assert result.tool_call.args == {"name": "chrome"}
    assert result.tool_call.call_id == "c1"
    assert result.reply == "Chrome khol raha hoon."
    assert "set_volume" in caplog.text and "close_app" in caplog.text


def test_brain_only_tools_run_inside_brain(mem: Memory, settings: Settings) -> None:
    llm = StubLLM(msg(
        None,
        tool_call("remember_fact", {"key": "Name", "value": "Harsh"}, "c1"),
        tool_call("open_app", {"name": "notepad"}, "c2"),
    ))
    result = make(mem, settings, llm).decide("Mera naam Harsh hai, notepad kholo", None)
    assert mem.recall("name") == "Harsh"
    assert result.tool_call is not None and result.tool_call.name == "open_app"
    assert result.reply == "Theek hai, kar raha hoon."


def test_remember_fact_alone_gets_phrase(mem: Memory, settings: Settings) -> None:
    llm = StubLLM(msg(None, tool_call("remember_fact", {"key": "city", "value": "Pune"})))
    result = make(mem, settings, llm).decide("Main Pune mein rehta hoon", None)
    assert result.tool_call is None
    assert result.reply == "Theek hai, yaad rakh liya."
    assert mem.recall("city") == "Pune"


def test_facts_in_context(mem: Memory, settings: Settings) -> None:
    mem.remember("name", "Harsh")
    llm = StubLLM(msg("Aapka naam Harsh hai."))
    make(mem, settings, llm).decide("Mera naam kya hai?", None)
    assert "name: Harsh" in llm.calls[0][0]["content"]


def test_clear_conversation_skips_recording(mem: Memory, settings: Settings) -> None:
    llm = StubLLM(msg("hello"), msg(None, tool_call("clear_conversation", {})))
    brain = make(mem, settings, llm)
    first = brain.decide("hello", None)
    brain.record_turn("hello", first.reply, first.language)
    mem.remember("name", "Harsh")
    result = brain.decide("sab bhool jao", "hi-IN")
    assert result.reply == "Theek hai, sab bhool gaya."
    brain.record_turn("sab bhool jao", result.reply, result.language)
    assert mem.recent_messages() == []
    assert mem.recall("name") == "Harsh"


def test_empty_reply_with_tool_is_in_progress(mem: Memory, settings: Settings) -> None:
    llm = StubLLM(msg(None, tool_call("open_app", {"name": "notepad"})))
    result = make(mem, settings, llm).decide("नोटपैड खोलो", None)
    assert result.reply == "ठीक है, कर रहा हूँ।"


def test_bad_json_arguments_are_dropped(mem: Memory, settings: Settings) -> None:
    llm = StubLLM(msg(None, tool_call("open_app", "{not json")))
    result = make(mem, settings, llm).decide("Open notepad", None)
    assert result.tool_call is None
    assert result.reply == "Sorry, please say that again."


def test_missing_call_id_gets_one(mem: Memory, settings: Settings) -> None:
    call = tool_call("open_app", {"name": "chrome"})
    del call["id"]
    result = make(mem, settings, StubLLM(msg("ok", call))).decide("open chrome", None)
    assert result.tool_call is not None and result.tool_call.call_id


def test_parse_arguments() -> None:
    assert parse_arguments('{"a": 1}') == {"a": 1}
    assert parse_arguments({"a": 1}) == {"a": 1}
    assert parse_arguments("") == {} and parse_arguments(None) == {}
    assert parse_arguments("[1, 2]") is None
    assert parse_arguments("{bad") is None


def test_llm_error_gives_apology(mem: Memory, settings: Settings) -> None:
    llm = StubLLM(SarvamError("HTTP 503: overloaded", 503), msg("ok"))
    brain = make(mem, settings, llm)
    result = brain.decide("Chrome kholo", None)
    assert result.reply == "Maaf kijiye, phir se boliye."
    assert result.language == "hi-IN" and result.tool_call is None
    assert brain.health().ok is False
    brain.decide("Chrome kholo", None)
    assert brain.health().ok is True


def test_malformed_llm_message_gives_apology(mem: Memory, settings: Settings) -> None:
    llm = StubLLM({"role": "assistant", "content": None, "tool_calls": [42]})
    result = make(mem, settings, llm).decide("hello there", None)
    assert result.reply == "Sorry, please say that again."


class SlowClient:
    """A SarvamClient stand-in whose request never finishes in time."""

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        time.sleep(3)
        return {"choices": [{"message": {"role": "assistant", "content": "late"}}]}


def test_llm_timeout_gives_apology(mem: Memory, settings: Settings) -> None:
    llm = SarvamLLM(SlowClient(), "sarvam-105b", timeout_s=0.2)  # type: ignore[arg-type]
    start = time.perf_counter()
    result = make(mem, settings, llm).decide("Chrome kholo", None)
    assert time.perf_counter() - start < 2
    assert result.reply == "Maaf kijiye, phir se boliye."
    assert result.tool_call is None
    assert result.llm_ms >= 150


def test_cancelled_before_llm_skips_call(mem: Memory, settings: Settings) -> None:
    llm = StubLLM()
    token = CancelToken()
    token.cancel()
    result = make(mem, settings, llm).decide("Chrome kholo", None, token)
    assert llm.calls == []
    assert result.tool_call is None
    assert result.reply == "Theek hai, cancel kar diya."


def test_cancelled_during_llm_still_returns(mem: Memory, settings: Settings) -> None:
    token = CancelToken()
    llm = StubLLM(msg("Chrome khol raha hoon.", tool_call("open_app", {"name": "chrome"})))
    llm.on_call = token.cancel
    result = make(mem, settings, llm).decide("Chrome kholo", None, token)
    assert token.cancelled
    assert result.tool_call is not None and result.tool_call.name == "open_app"


# -- language -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, stt, code, romanised",
    [
        ("नोटपैड खोलो", None, "hi-IN", False),
        ("Chrome kholo", None, "hi-IN", True),
        ("Open the calculator please", None, "en-IN", False),
        ("நோட்பேட் திற", None, "ta-IN", False),
        ("నోట్‌ప్యాడ్ తెరువు", None, "te-IN", False),
        ("ক্রোম খোলো", None, "bn-IN", False),
        ("नोटपैड उघड", "mr-IN", "mr-IN", False),
    ],
)
def test_language_and_script(
    mem: Memory, settings: Settings, text: str, stt: str | None, code: str, romanised: bool
) -> None:
    result = make(mem, settings, StubLLM(msg("ok"))).decide(text, stt)
    assert (result.language, result.romanised) == (code, romanised)


def test_language_hint_used_without_stt_code(mem: Memory, settings: Settings) -> None:
    hinted = replace(settings, language_hint="ta-IN")
    llm = StubLLM(msg("ok"), msg("ok"))
    brain = SarvamBrain(mem, llm, hinted, ACTION_SCHEMAS)
    assert brain.decide("hello", None).language == "ta-IN"
    assert brain.decide("hello", "kn-IN").language == "kn-IN"


def test_apology_in_users_script(mem: Memory, settings: Settings) -> None:
    llm = StubLLM(SarvamError("down"))
    result = make(mem, settings, llm).decide("நோட்பேட் திற", None)
    assert result.reply == "மன்னிக்கவும், மீண்டும் சொல்லுங்கள்."


# -- memory ---------------------------------------------------------------------------


def test_history_is_limited_to_history_turns(mem: Memory, settings: Settings) -> None:
    llm = StubLLM(msg("a3"))
    brain = SarvamBrain(mem, llm, replace(settings, history_turns=1))
    for i in (1, 2):
        brain.record_turn(f"q{i}", f"a{i}", "en-IN")
    brain.decide("q3", None)
    assert llm.calls[0][1:] == [
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "q3"},
    ]


def test_last_action_resolves_isko(mem: Memory, settings: Settings) -> None:
    llm = StubLLM(msg("Chrome band kar raha hoon.", tool_call("close_app", {"name": "chrome"})))
    brain = make(mem, settings, llm)
    brain.record_action(outcome("open_app", "ok", "Opened chrome", args={"name": "chrome"}))
    result = brain.decide("ab isko band karo", None)
    assert 'Last action: open_app({"name": "chrome"})' in llm.calls[0][0]["content"]
    assert result.tool_call is not None and result.tool_call.args == {"name": "chrome"}


@pytest.mark.parametrize("status", ["ok", "error", "denied", "blocked", "timeout", "cancelled"])
def test_record_action_every_status(mem: Memory, settings: Settings, status: str) -> None:
    brain = make(mem, settings, StubLLM())
    brain.record_action(outcome("open_app", status, "x", args={"name": "chrome"}, duration_ms=7))
    rows = mem._query("SELECT name, status, duration_ms FROM actions")
    assert [tuple(r) for r in rows] == [("open_app", status, 7)]


def test_record_action_details(mem: Memory, settings: Settings) -> None:
    brain = make(mem, settings, StubLLM())
    brain.record_action(outcome("system_status", "ok", {"battery_percent": 50}))
    brain.record_action(outcome("set_volume", "ok", "Volume set to 40%", fact="volume 40"))
    brain.record_action(outcome("open_app", "weird", None))
    brain.record_action(outcome("remember_fact", "ok", "Saved"))
    rows = [tuple(r) for r in mem._query("SELECT name, result, status FROM actions")]
    assert rows == [
        ("system_status", '{"battery_percent": 50}', "ok"),
        ("set_volume", "volume 40", "ok"),
        ("open_app", "", "error"),
    ]


def test_record_turn_never_raises(mem: Memory, settings: Settings) -> None:
    brain = make(mem, settings, StubLLM())
    mem.close()
    brain.record_turn("hi", "hello", "en-IN")
    brain.record_action(outcome("open_app"))


# -- summarise ------------------------------------------------------------------------


def summary(o: ActionResult, language: str = "hi-IN", romanised: bool = True) -> str:
    return summarise_outcome(o, language, romanised)


def test_summarise_uses_result_language(mem: Memory, settings: Settings) -> None:
    brain = make(mem, settings, StubLLM())
    opened = outcome("open_app", args={"name": "chrome"})
    assert brain.summarise(BrainResult("", "hi-IN", True), opened) == "Chrome khul gaya."
    assert brain.summarise(BrainResult("", "hi-IN", False), opened) == "Chrome खुल गया।"
    assert brain.summarise(BrainResult("", "en-IN"), opened) == "Chrome is open now."
    assert brain.summarise(BrainResult("", "ta-IN"), opened) == "Chrome திறக்கப்பட்டது."


@pytest.mark.parametrize(
    "o, expected",
    [
        (outcome("open_app", "error", "boom", args={"name": "chrome"}),
         "Maaf kijiye, yeh kaam nahi ho paya."),
        (outcome("open_app", "error", "chrome is not installed", args={"name": "chrome"}),
         "Chrome is computer par install nahi hai."),
        (outcome("open_app", "error", "", args={"name": "word"}, extra={"reason": "not_installed"}),
         "Word is computer par install nahi hai."),
        (outcome("shutdown_pc", "denied"), "Theek hai, maine nahi kiya."),
        (outcome("delete_all", "blocked"), "Maaf kijiye, yeh kaam main nahi kar sakta."),
        (outcome("open_app", "blocked", extra={"rate_limited": True}),
         "Bahut saare kaam ek saath ho gaye, thoda rukiye."),
        (outcome("open_app", "timeout"),
         "Maaf kijiye, ismein bahut der lag gayi. Phir se koshish kijiye."),
        (outcome("open_app", "cancelled"), "Theek hai, cancel kar diya."),
        (outcome("open_app", "mystery"), "Maaf kijiye, yeh kaam nahi ho paya."),
    ],
)
def test_summarise_failure_statuses(o: ActionResult, expected: str) -> None:
    assert summary(o) == expected


@pytest.mark.parametrize(
    "o, expected",
    [
        (outcome("close_app", args={"name": "file_manager"}), "File Manager band kar diya."),
        (outcome("close_app", result="chrome is not running", args={"name": "chrome"}),
         "Chrome khula nahi hai."),
        (outcome("set_volume", result="Volume set to 40%", args={"level": 40}),
         "Volume ab 40 percent hai."),
        (outcome("change_volume", fact="volume 70", args={"direction": "up"}),
         "Volume ab 70 percent hai."),
        (outcome("change_volume", args={"direction": "mute"}), "Awaaz band kar di."),
        (outcome("change_volume", args={"direction": "unmute"}), "Awaaz phir se chalu kar di."),
        (outcome("web_search", args={"query": "train ticket"}),
         "train ticket ke results khol diye."),
        (outcome("youtube_search", args={"query": "bhajan"}), "YouTube par bhajan khol diya."),
        (outcome("type_text"), "Likh diya."),
        (outcome("take_screenshot"), "Screenshot save ho gaya."),
        (outcome("lock_pc"), "Computer lock kar diya."),
        (outcome("save_note"), "Note save kar diya."),
        (outcome("shutdown_pc", result="Shutdown in 120 seconds."),
         "Computer 120 second mein band ho jayega. Rokne ke liye shutdown cancel boliye."),
        (outcome("shutdown_pc"),
         "Computer 60 second mein band ho jayega. Rokne ke liye shutdown cancel boliye."),
        (outcome("cancel_shutdown"), "Shutdown rok diya."),
        (outcome("cancel_shutdown", result="No shutdown was scheduled"),
         "Koi shutdown set nahi tha."),
        (outcome("press_shortcut", args={"shortcut": "save"}), "Ho gaya."),
    ],
)
def test_summarise_success_templates(o: ActionResult, expected: str) -> None:
    assert summary(o) == expected


def test_summarise_informational_fills_numbers() -> None:
    status = outcome("system_status", result={
        "battery_percent": 80, "charging": True, "cpu_percent": 12, "memory_percent": 55,
    }, informational=True)
    assert summary(status, romanised=False) == (
        "बैटरी 80 प्रतिशत है, सीपीयू 12 प्रतिशत और मेमोरी 55 प्रतिशत।"
    )
    assert summary(status, "en-IN", False) == (
        "Battery is at 80 percent, CPU 12 percent and memory 55 percent."
    )
    telugu = summary(status, "te-IN", False)
    assert "80" in telugu and "శాతం" in telugu
    desktop = outcome("system_status", result={
        "battery_percent": None, "cpu_percent": 5, "memory_percent": 40,
    })
    assert summary(desktop) == (
        "Is computer mein battery nahi hai. CPU 5 percent aur memory 40 percent hai."
    )
    clock = outcome("current_time", result={"time": "10:30", "date": "Thursday, 08 October 2026"})
    assert summary(clock) == "Abhi 10:30 baje hain, aaj Thursday, 08 October 2026 hai."
    assert summary(clock, "ta-IN", False).startswith("இப்போது நேரம் 10:30")


def test_summarise_unknown_action_uses_fact_only_in_english() -> None:
    o = outcome("brightness", fact="brightness 70")
    assert summary(o, "en-IN", False) == "Brightness 70."
    assert summary(o, "hi-IN", True) == "Ho gaya."
    assert summary(outcome("brightness"), "en-IN", False) == "Done."


# -- lifecycle ------------------------------------------------------------------------


def test_lifecycle_and_health(mem: Memory, settings: Settings) -> None:
    brain = make(mem, settings, StubLLM())
    assert brain.health().module == "brain"
    brain.start()
    assert brain.health().ok and brain.health().detail == "ready"
    brain.stop()
    assert brain.health().detail == "stopped"

"""Tests for FakeLLM (keyword rules, tools-driven names) and FakeBrain."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from brain.brain import SarvamBrain
from brain.fake import SAMPLE_LEAVE_LETTER, UNKNOWN_REPLY, FakeBrain, FakeLLM, MockLLM
from brain.memory import Memory
from core.config import Settings
from core.events import ActionResult, BrainResult, ToolCall


def schema(tool: str, /, **enums: list[str]) -> dict[str, Any]:
    props = {k: {"type": "string", "enum": v} for k, v in enums.items()}
    return {"type": "function", "function": {
        "name": tool, "parameters": {"type": "object", "properties": props}}}


TOOLS = [
    schema("open_app", name=["notepad", "chrome", "vlc"]),
    schema("close_app", name=["notepad", "chrome"]),
    schema("press_shortcut", shortcut=["save", "copy"]),
    schema("system_status"),
    schema("remember_fact"),
    schema("clear_conversation"),
]


@pytest.fixture
def mem() -> Iterator[Memory]:
    with Memory(":memory:") as m:
        yield m


def ask(llm: FakeLLM, text: str, tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return llm.chat([{"role": "user", "content": text}], TOOLS if tools is None else tools)


def call_of(message: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    (call,) = message["tool_calls"]
    assert call["id"].startswith("mock_call_")
    return call["function"]["name"], json.loads(call["function"]["arguments"])


def test_alias_and_constants() -> None:
    assert MockLLM is FakeLLM
    assert "अवकाश" in SAMPLE_LEAVE_LETTER
    assert UNKNOWN_REPLY.startswith("Mock mode")


def test_app_names_come_from_tools(mem: Memory) -> None:
    llm = FakeLLM(mem)
    assert call_of(ask(llm, "vlc kholo")) == ("open_app", {"name": "vlc"})
    assert call_of(ask(llm, "नोटपैड खोलो")) == ("open_app", {"name": "notepad"})
    # paint is a known word but not offered by the open_app tool
    assert ask(llm, "paint kholo") == {"role": "assistant", "content": UNKNOWN_REPLY}


def test_closable_apps_come_from_tools(mem: Memory) -> None:
    llm = FakeLLM(mem)
    assert call_of(ask(llm, "chrome band karo")) == ("close_app", {"name": "chrome"})
    assert "band nahi kar sakta" in ask(llm, "vlc band karo")["content"]


def test_shortcuts_come_from_tools(mem: Memory) -> None:
    llm = FakeLLM(mem)
    assert call_of(ask(llm, "save karo")) == ("press_shortcut", {"shortcut": "save"})
    assert ask(llm, "undo karo")["content"] == UNKNOWN_REPLY


def test_tool_not_offered_gives_plain_reply(mem: Memory) -> None:
    llm = FakeLLM(mem)
    assert ask(llm, "screenshot lo") == {"role": "assistant", "content": UNKNOWN_REPLY}


def test_no_tools_means_defaults(mem: Memory) -> None:
    llm = FakeLLM(mem)
    assert call_of(ask(llm, "paint kholo", [])) == ("open_app", {"name": "paint"})
    assert call_of(ask(llm, "screenshot lo", [])) == ("take_screenshot", {})
    mem.log_action("open_app", {"name": "file_manager"}, "Opened", "ok")
    assert "tool_calls" not in ask(llm, "isko band karo", [])


def test_replies_are_in_progress(mem: Memory) -> None:
    llm = FakeLLM(mem)
    for text in ("chrome kholo", "volume 30", "likho: hello", "screenshot lo",
                 "chhutti ki application likho"):
        content = ask(llm, text, [])["content"] or ""
        assert "diya" not in content and "liya" not in content and " di." not in content


def test_isko_band_karo_uses_memory(mem: Memory) -> None:
    llm = FakeLLM(mem)
    mem.log_action("open_app", {"name": "chrome"}, "Opened chrome", "ok")
    assert call_of(ask(llm, "ab isko band karo")) == ("close_app", {"name": "chrome"})


def test_fake_llm_drives_real_brain(mem: Memory, settings: Settings) -> None:
    brain = SarvamBrain(mem, FakeLLM(mem), settings, TOOLS[:4])
    first = brain.decide("Chrome kholo", None)
    assert first.tool_call == ToolCall("open_app", {"name": "chrome"}, first.tool_call.call_id)
    assert first.reply == "chrome khol raha hoon."
    brain.record_action(ActionResult("open_app", {"name": "chrome"}, "ok", "Opened chrome"))
    brain.record_turn("Chrome kholo", first.reply, first.language)
    second = brain.decide("ab isko band karo", None)
    assert second.tool_call is not None
    assert (second.tool_call.name, second.tool_call.args) == ("close_app", {"name": "chrome"})
    named = brain.decide("mera naam Harsh hai", None)
    assert named.tool_call is None and mem.recall("name") == "Harsh"


def test_fake_brain_scripted() -> None:
    scripted = BrainResult("Chrome khol raha hoon.", "hi-IN", True,
                           ToolCall("open_app", {"name": "chrome"}, "c1"))
    brain = FakeBrain({"Chrome kholo": scripted}, summaries={"lock_pc": "Locked!"})
    result = brain.decide("Chrome kholo", "hi-IN")
    assert result == scripted and result is not scripted
    unknown = brain.decide("kuch bhi", None)
    assert unknown.reply == UNKNOWN_REPLY and unknown.tool_call is None
    assert unknown.language == "hi-IN"
    assert brain.decisions == [("Chrome kholo", "hi-IN"), ("kuch bhi", None)]
    opened = ActionResult("open_app", {"name": "chrome"}, "ok", "Opened chrome")
    assert brain.summarise(result, opened) == "Chrome khul gaya."
    assert brain.summarise(result, ActionResult("lock_pc", {}, "ok", "")) == "Locked!"
    brain.record_turn("Chrome kholo", "Chrome khul gaya.", "hi-IN")
    brain.record_action(opened)
    assert brain.turns == [("Chrome kholo", "Chrome khul gaya.", "hi-IN")]
    assert brain.actions == [opened]


def test_fake_brain_callable_and_lifecycle() -> None:
    brain = FakeBrain(lambda text, lang: BrainResult(text.upper(), lang or "en-IN"))
    assert brain.decide("hello", None).reply == "HELLO"
    brain.start()
    assert brain.running and brain.health().ok
    brain.stop()
    assert not brain.running

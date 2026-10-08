"""Tests for the brain with a fake LLM (no network)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from typing import Any
from unittest.mock import MagicMock

import pytest

import safety.guard as safety
from actions.registry import ACTIONS
from brain.brain import Brain
from brain.memory import Memory
from core.config import Settings
from sarvam.client import SarvamError


class FakeLLM:
    """Returns scripted assistant messages and records every request."""

    def __init__(self, *responses: dict[str, Any] | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []
        self.tools: list[Any] = []

    def chat(self, messages: list[dict[str, Any]], tools: Any = None) -> dict[str, Any]:
        self.calls.append(messages)
        self.tools.append(tools)
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


@pytest.fixture
def mem() -> Iterator[Memory]:
    with Memory(":memory:") as m:
        yield m


@pytest.fixture(autouse=True)
def _limits() -> None:
    safety.configure(1000)


@pytest.fixture
def mocked(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    """Replace the functions behind a few actions so nothing real runs."""
    mocks = {
        "open_app": MagicMock(return_value="Opened notepad"),
        "shutdown_pc": MagicMock(return_value="Shutdown in 60 seconds"),
        "system_status": MagicMock(return_value={"battery_percent": 80, "charging": True}),
        "set_volume": MagicMock(side_effect=OSError("no audio device")),
    }
    for name, mock in mocks.items():
        monkeypatch.setitem(ACTIONS, name, replace(ACTIONS[name], func=mock))
    return mocks


def make(mem: Memory, settings: Settings, llm: FakeLLM) -> Brain:
    return Brain(mem, llm, settings)


def test_plain_answer_no_tools(mem: Memory, settings: Settings) -> None:
    llm = FakeLLM(msg("भारत की राजधानी नई दिल्ली है।"))
    reply = make(mem, settings, llm).handle("भारत की राजधानी क्या है?")
    assert reply.text == "भारत की राजधानी नई दिल्ली है।"
    assert reply.language == "hi-IN"
    assert reply.actions == []
    assert "llm_ms" in reply.timings and "total_ms" in reply.timings
    assert [m["role"] for m in mem.recent_messages()] == ["user", "assistant"]


def test_tools_and_system_prompt_sent(mem: Memory, settings: Settings) -> None:
    llm = FakeLLM(msg("ok"))
    make(mem, settings, llm).handle("Chrome kholo")
    names = {t["function"]["name"] for t in llm.tools[0]}
    assert {"open_app", "remember_fact", "clear_conversation"} <= names
    system = llm.calls[0][0]
    assert system["role"] == "system"
    assert "Roman letters" in system["content"]
    assert llm.calls[0][-1] == {"role": "user", "content": "Chrome kholo"}


def test_tool_call_runs_action(
    mem: Memory, settings: Settings, mocked: dict[str, MagicMock]
) -> None:
    llm = FakeLLM(msg("Notepad khol diya.", tool_call("open_app", {"name": "notepad"})))
    reply = make(mem, settings, llm).handle("Notepad kholo")
    mocked["open_app"].assert_called_once_with(name="notepad")
    assert reply.text == "Notepad khol diya."
    assert reply.actions[0].status == "ok"
    assert len(llm.calls) == 1
    assert mem.last_action()["name"] == "open_app"  # type: ignore[index]


def test_empty_text_gets_localised_confirmation(
    mem: Memory, settings: Settings, mocked: dict[str, MagicMock]
) -> None:
    llm = FakeLLM(msg(None, tool_call("open_app", {"name": "notepad"})))
    assert make(mem, settings, llm).handle("Notepad kholo").text == "Ho gaya."


def test_denied_action_changes_reply(
    mem: Memory, settings: Settings, mocked: dict[str, MagicMock]
) -> None:
    llm = FakeLLM(msg("PC band ho raha hai.", tool_call("shutdown_pc", {})))
    asked: list[str] = []
    reply = make(mem, settings, llm).handle(
        "PC band karo", ask_user=lambda q: asked.append(q) or "nahi"
    )
    mocked["shutdown_pc"].assert_not_called()
    assert asked
    assert reply.actions[0].status == "denied"
    assert reply.text == "Theek hai, maine nahi kiya."
    assert "band ho raha" not in reply.text


def test_error_action_is_reported(
    mem: Memory, settings: Settings, mocked: dict[str, MagicMock]
) -> None:
    llm = FakeLLM(msg("Volume 30 kar diya.", tool_call("set_volume", {"level": 30})))
    reply = make(mem, settings, llm).handle("Volume 30 kar do")
    assert reply.actions[0].status == "error"
    assert reply.text == "Maaf kijiye, yeh kaam nahi ho paya."


def test_unknown_tool_blocked(mem: Memory, settings: Settings) -> None:
    llm = FakeLLM(msg("Done", tool_call("delete_everything", {})))
    reply = make(mem, settings, llm).handle("Delete all my files")
    assert reply.actions[0].status == "blocked"
    assert reply.text == "Sorry, I am not allowed to do that."


def test_bad_json_arguments(mem: Memory, settings: Settings) -> None:
    llm = FakeLLM(msg("ok", tool_call("open_app", "{not json")))
    reply = make(mem, settings, llm).handle("Open notepad")
    assert reply.actions[0].status == "error"


def test_informational_second_call(
    mem: Memory, settings: Settings, mocked: dict[str, MagicMock]
) -> None:
    llm = FakeLLM(
        msg(None, tool_call("system_status", {}, "call_9")),
        msg("बैटरी 80 प्रतिशत है और चार्ज हो रही है।"),
    )
    reply = make(mem, settings, llm).handle("बैटरी कितनी है?")
    assert reply.text == "बैटरी 80 प्रतिशत है और चार्ज हो रही है।"
    second = llm.calls[1]
    assert second[-2]["role"] == "assistant" and second[-2]["tool_calls"][0]["id"] == "call_9"
    assert second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "call_9"
    assert json.loads(second[-1]["content"])["result"]["battery_percent"] == 80


def test_remember_fact_stores_fact(mem: Memory, settings: Settings) -> None:
    llm = FakeLLM(
        msg("Theek hai Harsh.", tool_call("remember_fact", {"key": "Name", "value": "Harsh"})),
        msg("Aapka naam Harsh hai."),
    )
    brain = make(mem, settings, llm)
    brain.handle("Mera naam Harsh hai")
    assert mem.recall("name") == "Harsh"
    reply = brain.handle("Mera naam kya hai?")
    assert "name: Harsh" in llm.calls[1][0]["content"]
    assert reply.text == "Aapka naam Harsh hai."


def test_history_is_passed(mem: Memory, settings: Settings) -> None:
    llm = FakeLLM(msg("first answer"), msg("second answer"))
    brain = make(mem, settings, llm)
    brain.handle("first question")
    brain.handle("second question")
    second = llm.calls[1]
    assert second[1] == {"role": "user", "content": "first question"}
    assert second[2] == {"role": "assistant", "content": "first answer"}
    assert second[3] == {"role": "user", "content": "second question"}


def test_last_action_in_context(
    mem: Memory, settings: Settings, mocked: dict[str, MagicMock]
) -> None:
    llm = FakeLLM(msg("ok", tool_call("open_app", {"name": "notepad"})), msg("?"))
    brain = make(mem, settings, llm)
    brain.handle("Notepad kholo")
    brain.handle("ab isko band karo")
    assert "Last action: open_app" in llm.calls[1][0]["content"]


def test_llm_error_gives_fallback(mem: Memory, settings: Settings) -> None:
    llm = FakeLLM(SarvamError("HTTP 503: overloaded", 503))
    reply = make(mem, settings, llm).handle("Chrome kholo")
    assert reply.text == "Maaf kijiye, phir se boliye."
    assert reply.language == "hi-IN"


def test_clear_conversation(mem: Memory, settings: Settings) -> None:
    llm = FakeLLM(msg("hello"), msg("Sab bhool gaya.", tool_call("clear_conversation", {})))
    brain = make(mem, settings, llm)
    brain.handle("hello")
    mem.remember("name", "Harsh")
    brain.handle("sab bhool jao")
    assert mem.recent_messages() == []
    assert mem.recall("name") == "Harsh"


def test_tool_call_limit(mem: Memory, settings: Settings, mocked: dict[str, MagicMock]) -> None:
    calls = [tool_call("open_app", {"name": "notepad"}, f"c{i}") for i in range(9)]
    llm = FakeLLM(msg("ok", *calls))
    reply = make(mem, settings, llm).handle("open notepad many times")
    assert len(reply.actions) == 5

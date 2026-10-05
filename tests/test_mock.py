"""Tests for --mock mode: MockLLM rules, MockTTS, and the mock wiring."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Iterator
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import assistant.safety as safety
from assistant.actions import ACTIONS, PROCESS_NAMES
from assistant.brain import Brain
from assistant.config import MOCK_API_KEY, ConfigError, Settings, load_settings
from assistant.memory import Memory
from sarvam.mock import SAMPLE_LEAVE_LETTER, UNKNOWN_REPLY, MockLLM, MockTTS


@pytest.fixture
def mem() -> Iterator[Memory]:
    with Memory(":memory:") as m:
        yield m


@pytest.fixture
def llm(mem: Memory) -> MockLLM:
    return MockLLM(mem)


def ask(llm: MockLLM, text: str) -> dict[str, Any]:
    return llm.chat([{"role": "system", "content": "x"}, {"role": "user", "content": text}], [])


def tool(message: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Name and decoded arguments of the single tool call."""
    calls = message["tool_calls"]
    assert len(calls) == 1
    call = calls[0]
    assert call["type"] == "function" and call["id"].startswith("mock_call_")
    assert isinstance(call["function"]["arguments"], str)  # JSON string, like the real API
    return call["function"]["name"], json.loads(call["function"]["arguments"])


@pytest.mark.parametrize(
    "text, name, args",
    [
        ("notepad kholo", "open_app", {"name": "notepad"}),
        ("Notepad kholo", "open_app", {"name": "notepad"}),
        ("नोटपैड खोलो", "open_app", {"name": "notepad"}),
        ("Chrome open karo", "open_app", {"name": "chrome"}),
        ("calculator", "open_app", {"name": "calculator"}),
        ("file manager kholo", "open_app", {"name": "file_manager"}),
        ("chrome band karo", "close_app", {"name": "chrome"}),
        ("Google pe train ticket search karo", "web_search", {"query": "train ticket"}),
        ("YouTube pe bhajan lagao", "youtube_search", {"query": "bhajan"}),
        ("volume 30", "set_volume", {"level": 30}),
        ("Volume 30 kar do", "set_volume", {"level": 30}),
        ("volume 250 kar do", "set_volume", {"level": 100}),
        ("awaaz badhao", "change_volume", {"direction": "up", "step": 10}),
        ("volume kam karo", "change_volume", {"direction": "down", "step": 10}),
        ("mute karo", "change_volume", {"direction": "mute"}),
        ("unmute karo", "change_volume", {"direction": "unmute"}),
        ("likho: kal meeting hai", "type_text", {"text": "kal meeting hai"}),
        ("save karo", "press_shortcut", {"shortcut": "save"}),
        ("naya tab kholo", "press_shortcut", {"shortcut": "new_tab"}),
        ("zoom in karo", "press_shortcut", {"shortcut": "zoom_in"}),
        ("neeche scroll karo", "scroll", {"direction": "down", "amount": "medium"}),
        ("thoda upar scroll karo", "scroll", {"direction": "up", "amount": "small"}),
        ("screenshot lo", "take_screenshot", {}),
        ("Battery kitni hai?", "system_status", {}),
        ("बैटरी कितनी है?", "system_status", {}),
        ("kitne baje hain?", "current_time", {}),
        ("computer lock karo", "lock_pc", {}),
        ("PC band karo", "shutdown_pc", {}),
        ("shutdown cancel karo", "cancel_shutdown", {}),
        ("mera naam Harsh hai", "remember_fact", {"key": "name", "value": "Harsh"}),
        ("मेरा नाम हर्ष है", "remember_fact", {"key": "name", "value": "हर्ष"}),
        ("sab bhool jao", "clear_conversation", {}),
    ],
)
def test_rules(llm: MockLLM, text: str, name: str, args: dict[str, Any]) -> None:
    assert tool(ask(llm, text)) == (name, args)


def test_every_action_reachable(llm: MockLLM) -> None:
    """Each real action has at least one mock phrase, and its args pass validation."""
    phrases = [
        "notepad kholo", "chrome band karo", "google pe news search karo", "youtube pe gaane",
        "volume 30", "awaaz badhao", "likho: hello", "copy karo", "neeche scroll karo",
        "screenshot lo", "battery kitni hai", "computer lock karo", "pc band karo",
        "shutdown cancel karo", "chhutti ki application likho", "kitne baje hain",
    ]
    reached = set()
    for text in phrases:
        name, args = tool(ask(llm, text))
        reached.add(name)
        safety.validate_args(ACTIONS[name], args)  # raises if the mock sends bad args
    assert reached == set(ACTIONS)


def test_application_letter(llm: MockLLM) -> None:
    name, args = tool(ask(llm, "Chhutti ki application likho"))
    assert name == "save_note"
    assert args["content"] == SAMPLE_LEAVE_LETTER
    assert "अवकाश" in args["content"]


def test_isko_band_karo_uses_last_opened_app(llm: MockLLM, mem: Memory) -> None:
    mem.log_action("open_app", {"name": "paint"}, "Opened paint", "ok")
    assert tool(ask(llm, "ab isko band karo")) == ("close_app", {"name": "paint"})


def test_isko_band_karo_ignores_failed_open(llm: MockLLM, mem: Memory) -> None:
    mem.log_action("open_app", {"name": "chrome"}, "Opened chrome", "ok")
    mem.log_action("open_app", {"name": "paint"}, "boom", "error")
    assert tool(ask(llm, "isko band karo")) == ("close_app", {"name": "chrome"})


def test_isko_band_karo_without_history(llm: MockLLM) -> None:
    reply = ask(llm, "isko band karo")
    assert "tool_calls" not in reply
    assert "Mock mode" in reply["content"]


def test_isko_band_karo_cannot_close_file_manager(llm: MockLLM, mem: Memory) -> None:
    assert "file_manager" not in PROCESS_NAMES
    mem.log_action("open_app", {"name": "file_manager"}, "Opened", "ok")
    assert "tool_calls" not in ask(llm, "isko band karo")


def test_name_answer_from_memory(llm: MockLLM, mem: Memory) -> None:
    assert ask(llm, "mera naam kya hai?")["content"] == "Mujhe aapka naam nahi pata."
    mem.remember("name", "Harsh")
    reply = ask(llm, "Mera naam kya hai?")
    assert reply == {"role": "assistant", "content": "Aapka naam Harsh hai."}


def test_unknown_gives_plain_reply(llm: MockLLM) -> None:
    assert ask(llm, "aaj mausam kaisa hai") == {"role": "assistant", "content": UNKNOWN_REPLY}
    assert ask(llm, "") == {"role": "assistant", "content": UNKNOWN_REPLY}


def test_second_call_summarises_tool_results(llm: MockLLM) -> None:
    first = ask(llm, "battery kitni hai")
    call = first["tool_calls"][0]
    follow = [
        {"role": "user", "content": "battery kitni hai"},
        first,
        {"role": "tool", "tool_call_id": call["id"], "content": json.dumps({
            "status": "ok",
            "result": {"battery_percent": 58, "charging": True, "cpu_percent": 9,
                       "memory_percent": 60},
        })},
    ]
    text = llm.chat(follow, [])["content"]
    assert "Battery 58 percent" in text and "charge ho rahi hai" in text


def test_tool_call_ids_unique(llm: MockLLM) -> None:
    ids = {ask(llm, "notepad kholo")["tool_calls"][0]["id"] for _ in range(3)}
    assert len(ids) == 3


# -- through the real Brain + safety gate -------------------------------------------


@pytest.fixture
def mocked_actions(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    mocks = {
        "open_app": MagicMock(return_value="Opened notepad"),
        "close_app": MagicMock(return_value="Closed notepad"),
        "shutdown_pc": MagicMock(return_value="Shutdown in 60 seconds"),
        "system_status": MagicMock(return_value={"battery_percent": 77, "charging": False,
                                                 "cpu_percent": 5, "memory_percent": 40}),
    }
    for name, mock in mocks.items():
        monkeypatch.setitem(ACTIONS, name, replace(ACTIONS[name], func=mock))
    safety.configure(1000)
    return mocks


def test_brain_with_mock_llm_end_to_end(
    mem: Memory, settings: Settings, mocked_actions: dict[str, MagicMock]
) -> None:
    brain = Brain(mem, MockLLM(mem), settings)
    assert brain.handle("Notepad kholo").actions[0].status == "ok"
    mocked_actions["open_app"].assert_called_once_with(name="notepad")
    brain.handle("ab isko band karo", ask_user=lambda q: "haan")
    mocked_actions["close_app"].assert_called_once_with(name="notepad")
    brain.handle("mera naam Harsh hai")
    assert brain.handle("mera naam kya hai").text == "Aapka naam Harsh hai."
    assert "77 percent" in brain.handle("battery kitni hai").text
    denied = brain.handle("PC band karo", ask_user=lambda q: "nahi")
    assert denied.actions[0].status == "denied"
    mocked_actions["shutdown_pc"].assert_not_called()
    assert brain.handle("kuch bhi random").text == UNKNOWN_REPLY


# -- MockTTS -------------------------------------------------------------------------


def test_mock_tts_prints_without_pyttsx3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pyttsx3", None)  # import raises ImportError
    printed: list[str] = []
    tts = MockTTS(output=printed.append)
    assert list(tts.stream("नोटपैड खोल दिया", "hi-IN")) == []
    assert printed == ["[voice] नोटपैड खोल दिया"]


def test_mock_tts_speaks_with_pyttsx3(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = MagicMock()
    monkeypatch.setitem(sys.modules, "pyttsx3", SimpleNamespace(init=lambda: engine))
    MockTTS(output=lambda s: None).speak("hello")
    engine.say.assert_called_once_with("hello")
    engine.runAndWait.assert_called_once()


def test_mock_tts_survives_voice_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> Any:
        raise RuntimeError("no SAPI voice")

    monkeypatch.setitem(sys.modules, "pyttsx3", SimpleNamespace(init=broken))
    MockTTS(output=lambda s: None).speak("hello")


# -- wiring --------------------------------------------------------------------------


def test_mock_settings_need_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)
    assert load_settings(env_file=None, require_key=False).sarvam_api_key == MOCK_API_KEY
    with pytest.raises(ConfigError):
        load_settings(env_file=None)


def test_mock_pipeline_typed_turn_and_confirmation(
    mem: Memory, settings: Settings, mocked_actions: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from assistant.pipeline import create_mock_pipeline

    monkeypatch.setitem(sys.modules, "pyttsx3", None)
    pipe = create_mock_pipeline(replace(settings, confirm_timeout_s=3), Brain(mem, MockLLM(mem),
                                                                               settings))
    pipe.player._factory = lambda sr: (_ for _ in ()).throw(AssertionError("no audio device"))
    try:
        pipe.submit_typed("Notepad kholo")
        _wait_timing(pipe)
        mocked_actions["open_app"].assert_called_once_with(name="notepad")
        pipe.submit_typed("PC band karo")
        deadline = time.monotonic() + 3
        while not pipe._confirm_pending.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        pipe.submit_typed("haan")
        _wait_timing(pipe)
        mocked_actions["shutdown_pc"].assert_called_once()
        assert not pipe.start_live() or _saw_error(pipe)
    finally:
        pipe.shutdown()


def _wait_timing(pipe: Any) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            if pipe.events.get(timeout=0.05).kind == "timing":
                return
        except Exception:  # noqa: BLE001 - queue.Empty
            continue
    raise AssertionError("no timing event")


def _saw_error(pipe: Any) -> bool:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            event = pipe.events.get(timeout=0.05)
        except Exception:  # noqa: BLE001 - queue.Empty
            continue
        if event.kind == "error":
            return "mock mode" in event.data
    return False


def test_mock_window(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    import tkinter

    from ui.app import AssistantApp

    submitted: list[str] = []
    pipe = MagicMock()
    pipe.events.get_nowait.side_effect = __import__("queue").Empty
    pipe.submit_typed.side_effect = submitted.append
    try:
        app = AssistantApp(pipe, settings, start_live=True, mock=True)
    except tkinter.TclError as exc:
        pytest.skip(f"no display: {exc}")
    try:
        app.withdraw()
        pipe.start_live.assert_not_called()
        assert app.live_switch.cget("state") == "disabled"
        app.entry.insert(0, "Notepad kholo")
        app._send_typed()
        assert submitted == ["Notepad kholo"]
        assert app.entry.get() == ""
    finally:
        app.on_close()

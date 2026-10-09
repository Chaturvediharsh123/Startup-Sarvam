"""Tests for the registry and each action function. Nothing real happens on the PC."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from actions import registry, system
from actions.notes import MAX_TITLE_CHARS, sanitize_filename, save_note
from actions.registry import ACTIONS, ActionError, NotAllowedError, tool_schemas
from actions.system import (
    cancel_shutdown,
    change_volume,
    current_time,
    lock_pc,
    set_volume,
    shutdown_pc,
    system_status,
    take_screenshot,
)
from actions.typing_keys import press_shortcut, scroll, type_text
from actions.web import web_search, youtube_search
from core.config import ALLOWLIST_FILE, Settings

EXPECTED = {
    "open_app", "close_app", "web_search", "youtube_search", "set_volume", "change_volume",
    "type_text", "press_shortcut", "scroll", "take_screenshot", "system_status", "lock_pc",
    "shutdown_pc", "cancel_shutdown", "save_note", "current_time",
}


# -- registry -----------------------------------------------------------------------


def test_all_actions_registered() -> None:
    assert set(ACTIONS) == EXPECTED


def test_risk_and_informational_flags() -> None:
    assert {n for n, s in ACTIONS.items() if s.risk == "high"} == {
        "close_app", "lock_pc", "shutdown_pc"
    }
    assert {n for n, s in ACTIONS.items() if s.risk == "medium"} == {
        "type_text", "press_shortcut", "save_note"
    }
    assert {n for n, s in ACTIONS.items() if s.informational} == {"system_status", "current_time"}


def test_registry_matches_allowlist_file() -> None:
    data = yaml.safe_load(ALLOWLIST_FILE.read_text(encoding="utf-8"))
    assert set(data["actions"]) == set(ACTIONS)
    for name, entry in data["actions"].items():
        spec = ACTIONS[name]
        assert entry["risk"] == spec.risk, name
        assert bool(entry.get("informational")) == spec.informational, name
        assert set(entry.get("args") or {}) == set(spec.parameters["properties"]), name


def test_tool_schemas_generated_from_registry() -> None:
    schemas = tool_schemas()
    assert [s["function"]["name"] for s in schemas] == list(ACTIONS)
    for tool in schemas:
        assert tool["type"] == "function"
        fn = tool["function"]
        assert set(fn) == {"name", "description", "parameters"}
        assert fn["description"] and "{" not in fn["description"]
        params = fn["parameters"]
        assert params["type"] == "object"
        assert params["additionalProperties"] is False
        assert set(params["required"]) <= set(params["properties"])
        for prop in params["properties"].values():
            assert prop["type"] in {"string", "integer", "boolean"}
            assert not any(k.startswith("x-") for k in prop)


def test_shortcut_enum_and_app_list_come_from_allowlist() -> None:
    by_name = {s["function"]["name"]: s["function"] for s in tool_schemas()}
    enum = by_name["press_shortcut"]["parameters"]["properties"]["shortcut"]["enum"]
    assert set(enum) == set(registry.safe_shortcuts())
    assert "chrome" in by_name["open_app"]["description"]
    assert "enum" not in by_name["open_app"]["parameters"]["properties"]["name"]


def test_adding_an_action_is_one_function(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "ACTIONS", dict(ACTIONS))

    @registry.action("Say hello.", {"who": {"type": "string"}}, required=["who"])
    def hello(who: str) -> str:
        return f"hello {who}"

    assert "hello" in [s["function"]["name"] for s in registry.tool_schemas()]


def test_decorator_rejects_bad_definitions() -> None:
    with pytest.raises(ValueError):
        registry.action("x", {}, required=["missing"])(lambda: None)
    with pytest.raises(ValueError):
        registry.action("x", risk="extreme")


def test_back_compat_views() -> None:
    assert registry.PROCESS_NAMES["chrome"] == ("chrome.exe",)
    assert "file_manager" not in registry.PROCESS_NAMES  # never kill explorer.exe
    assert registry.APPS["notepad"] == "notepad.exe"
    assert registry.SAFE_HOTKEYS["save"] == ("ctrl", "s")
    with pytest.raises(AttributeError):
        _ = registry.NOT_A_THING


def test_facts() -> None:
    assert ACTIONS["set_volume"].to_fact("volume 40") == "volume 40"
    assert ACTIONS["current_time"].to_fact({"time": "10:05", "date": "Mon"}) == "time 10:05, Mon"


# -- web ----------------------------------------------------------------------------


def test_searches_quote_query(no_side_effects: dict[str, MagicMock]) -> None:
    assert web_search("रेल टिकट & price").startswith("searched the web for")
    url = no_side_effects["browser"].call_args.args[0]
    assert url.startswith("https://www.google.com/search?q=")
    assert " " not in url and "&price" not in url
    youtube_search("arijit singh")
    assert no_side_effects["browser"].call_args.args[0].endswith("search_query=arijit+singh")


def test_search_rejects_empty_and_long(no_side_effects: dict[str, MagicMock]) -> None:
    with pytest.raises(ActionError):
        web_search("   ")
    with pytest.raises(ActionError):
        youtube_search("x" * 500)
    no_side_effects["browser"].assert_not_called()


# -- system -------------------------------------------------------------------------


def test_set_volume_clamps_and_unmutes(fake_volume: MagicMock) -> None:
    assert set_volume(30) == "volume 30"
    fake_volume.SetMasterVolumeLevelScalar.assert_called_with(0.3, None)
    fake_volume.SetMute.assert_called_with(0, None)
    assert set_volume(250) == "volume 100"
    with pytest.raises(ActionError):
        set_volume("loud")  # type: ignore[arg-type]


def test_change_volume(fake_volume: MagicMock) -> None:
    assert change_volume("up", 20) == "volume 70"
    assert change_volume("down") == "volume 40"
    assert change_volume("mute") == "volume muted"
    fake_volume.SetMute.assert_called_with(1, None)
    assert change_volume("unmute") == "volume unmuted"
    with pytest.raises(ActionError):
        change_volume("explode")


def test_take_screenshot(fake_pyautogui: MagicMock, settings: Settings) -> None:
    result = take_screenshot()
    saved = fake_pyautogui.screenshot.return_value.save.call_args.args[0]
    assert Path(saved).parent == settings.notes_dir
    assert str(saved) in result


def test_system_status(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = SimpleNamespace(
        sensors_battery=lambda: SimpleNamespace(percent=81.6, power_plugged=False),
        disk_usage=lambda p: SimpleNamespace(free=50e9, percent=40.2),
        cpu_percent=lambda interval: 12.4,
        virtual_memory=lambda: SimpleNamespace(percent=63.0),
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    status = system_status()
    assert status["battery_percent"] == 82
    assert status["charging"] is False
    fact = ACTIONS["system_status"].to_fact(status)
    assert fact.startswith("battery 82% not charging") and "cpu 12%" in fact


def test_system_status_without_battery(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = SimpleNamespace(
        sensors_battery=lambda: None,
        disk_usage=lambda p: SimpleNamespace(free=1e9, percent=1),
        cpu_percent=lambda interval: 1,
        virtual_memory=lambda: SimpleNamespace(percent=1),
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    status = system_status()
    assert status["battery_percent"] is None
    assert "battery" not in ACTIONS["system_status"].to_fact(status)


def test_lock_and_shutdown_use_fixed_commands(no_side_effects: dict[str, MagicMock]) -> None:
    assert lock_pc() == "pc locked"
    no_side_effects["lock"].assert_called_once()
    msg = shutdown_pc()
    cmd = no_side_effects["run"].call_args.args[0]
    assert cmd[0].lower().endswith("shutdown.exe")
    assert cmd[1:] == ["/s", "/t", "60"]
    assert "shell" not in no_side_effects["run"].call_args.kwargs
    assert "cancel" in msg


def test_shutdown_delay_never_below_a_minute(
    no_side_effects: dict[str, MagicMock], settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    monkeypatch.setattr(registry, "_settings", replace(settings, shutdown_delay_s=10))
    shutdown_pc()
    assert no_side_effects["run"].call_args.args[0][1:] == ["/s", "/t", "60"]
    monkeypatch.setattr(registry, "_settings", replace(settings, shutdown_delay_s=120))
    assert system.shutdown_delay() == 120


def test_cancel_shutdown(no_side_effects: dict[str, MagicMock]) -> None:
    no_side_effects["run"].return_value = subprocess.CompletedProcess([], 0)
    assert cancel_shutdown() == "shutdown cancelled"
    no_side_effects["run"].return_value = subprocess.CompletedProcess([], 1116)
    assert cancel_shutdown() == "no shutdown was scheduled"


def test_current_time() -> None:
    assert set(current_time()) == {"time", "date"}


# -- typing and keys ----------------------------------------------------------------


def test_type_text_pastes_and_restores_clipboard(
    fake_pyautogui: MagicMock, fake_pyperclip: MagicMock
) -> None:
    assert type_text("कल मीटिंग है") == "typed 12 characters"
    fake_pyautogui.hotkey.assert_called_once_with("ctrl", "v")
    fake_pyperclip.copy.assert_any_call("कल मीटिंग है")
    assert fake_pyperclip.store["text"] == "old clipboard"


def test_type_text_limits(fake_pyautogui: MagicMock, fake_pyperclip: MagicMock) -> None:
    with pytest.raises(ActionError):
        type_text("x" * 5000)
    with pytest.raises(ActionError):
        type_text("")
    fake_pyautogui.hotkey.assert_not_called()


def test_press_shortcut_and_scroll(fake_pyautogui: MagicMock) -> None:
    assert press_shortcut("save") == "pressed save"
    fake_pyautogui.hotkey.assert_called_with("ctrl", "s")
    scroll("down", "small")
    fake_pyautogui.scroll.assert_called_with(-360)
    scroll("up")
    fake_pyautogui.scroll.assert_called_with(960)
    with pytest.raises(ActionError):
        scroll("sideways")


@pytest.mark.parametrize("bad", ["alt+f4", "win+r", "close_window", "delete", ""])
def test_press_shortcut_refuses_unsafe(bad: str, fake_pyautogui: MagicMock) -> None:
    with pytest.raises(NotAllowedError):
        press_shortcut(bad)
    fake_pyautogui.hotkey.assert_not_called()


def test_press_shortcut_refuses_blocked_even_if_safe_list_edited(
    fake_pyautogui: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = registry.allowlist()
    monkeypatch.setitem(data["safe_shortcuts"], "sneaky", ["alt", "f4"])
    with pytest.raises(NotAllowedError):
        press_shortcut("sneaky")
    fake_pyautogui.hotkey.assert_not_called()


# -- notes --------------------------------------------------------------------------


def test_save_note_writes_utf8_and_opens(
    no_side_effects: dict[str, MagicMock], settings: Settings
) -> None:
    result = save_note("छुट्टी की अर्ज़ी", "प्रिय महोदय,\nमुझे छुट्टी चाहिए।")
    path = settings.notes_dir.resolve() / "छुट्टी की अर्ज़ी.txt"
    assert path.read_text(encoding="utf-8").startswith("प्रिय महोदय")
    assert str(path) in result
    no_side_effects["startfile"].assert_called_once_with(str(path))
    save_note("छुट्टी की अर्ज़ी", "second")  # same title must not overwrite
    assert path.read_text(encoding="utf-8").startswith("प्रिय")
    assert len(list(settings.notes_dir.glob("*.txt"))) == 2


def test_save_note_cannot_escape_folder(
    no_side_effects: dict[str, MagicMock], settings: Settings
) -> None:
    save_note("..\\..\\Windows\\evil", "x")
    files = list(settings.notes_dir.resolve().iterdir())
    assert len(files) == 1
    assert files[0].parent == settings.notes_dir.resolve()


def test_save_note_limits(no_side_effects: dict[str, MagicMock]) -> None:
    with pytest.raises(ActionError):
        save_note("t", "")
    with pytest.raises(ActionError):
        save_note("t", "x" * 30000)
    no_side_effects["startfile"].assert_not_called()


@pytest.mark.parametrize(
    "title, expected",
    [
        ("..\\..\\evil", "evil"),
        ("../etc/passwd", "etc passwd"),
        ("CON", "note_CON"),
        ("lpt1.txt", "note_lpt1.txt"),
        ("a<b>c:d|e?f*g", "a b c d e f g"),
        ("   ...   ", "note"),
        ("line\nbreak\x00", "line break"),
        ("छुट्टी की अर्ज़ी", "छुट्टी की अर्ज़ी"),
    ],
)
def test_sanitize_filename(title: str, expected: str) -> None:
    assert sanitize_filename(title) == expected


def test_sanitize_filename_length() -> None:
    assert len(sanitize_filename("x" * 500)) <= MAX_TITLE_CHARS

"""Tests for the action registry and each action. Nothing real happens on the PC."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import assistant.actions as actions
from assistant.actions import (
    ACTIONS,
    APPS,
    PROCESS_NAMES,
    SAFE_HOTKEYS,
    SCROLL_NOTCHES,
    sanitize_filename,
    tool_schemas,
)
from assistant.config import Settings

EXPECTED = {
    "open_app", "close_app", "web_search", "youtube_search", "set_volume", "change_volume",
    "type_text", "press_shortcut", "scroll", "take_screenshot", "system_status", "lock_pc",
    "shutdown_pc", "cancel_shutdown", "save_note", "current_time",
}


@pytest.fixture(autouse=True)
def _configured(settings: Settings) -> None:
    actions.configure(settings)
    actions._sleep = lambda _s: None


def test_all_actions_registered() -> None:
    assert set(ACTIONS) == EXPECTED


def test_risky_and_informational_flags() -> None:
    assert {n for n, s in ACTIONS.items() if s.risky} == {"close_app", "lock_pc", "shutdown_pc"}
    assert {n for n, s in ACTIONS.items() if s.informational} == {"system_status", "current_time"}


def test_tool_schemas_are_valid() -> None:
    schemas = tool_schemas()
    assert len(schemas) == len(ACTIONS)
    for tool in schemas:
        assert tool["type"] == "function"
        fn = tool["function"]
        assert set(fn) == {"name", "description", "parameters"}
        assert fn["description"]
        params = fn["parameters"]
        assert params["type"] == "object"
        assert params["additionalProperties"] is False
        assert set(params["required"]) <= set(params["properties"])
        for prop in params["properties"].values():
            assert prop["type"] in {"string", "integer", "boolean"}


def _enum(action: str, param: str) -> list[str]:
    return ACTIONS[action].parameters["properties"][param]["enum"]


def test_enums_match_maps() -> None:
    assert set(_enum("open_app", "name")) == set(APPS)
    assert set(_enum("close_app", "name")) == set(PROCESS_NAMES)
    assert set(_enum("press_shortcut", "shortcut")) == set(SAFE_HOTKEYS)
    assert set(_enum("scroll", "amount")) == set(SCROLL_NOTCHES)
    assert "file_manager" not in PROCESS_NAMES  # never kill explorer.exe


def test_decorator_rejects_undefined_required() -> None:
    with pytest.raises(ValueError):
        actions.action("x", {}, required=["missing"])(lambda: None)


def test_open_app_uses_startfile(no_side_effects: dict[str, MagicMock]) -> None:
    assert actions.open_app("notepad") == "Opened notepad"
    no_side_effects["startfile"].assert_called_once_with("notepad.exe")
    no_side_effects["popen"].assert_not_called()


def test_close_app_terminates_then_kills(monkeypatch: pytest.MonkeyPatch) -> None:
    chrome = MagicMock(info={"name": "chrome.exe"})
    stubborn = MagicMock(info={"name": "Chrome.exe"})
    other = MagicMock(info={"name": "notepad.exe"})
    fake = SimpleNamespace(
        process_iter=lambda attrs: [chrome, stubborn, other],
        wait_procs=MagicMock(return_value=([chrome], [stubborn])),
        NoSuchProcess=ProcessLookupError,
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    assert actions.close_app("chrome") == "Closed chrome"
    chrome.terminate.assert_called_once()
    stubborn.kill.assert_called_once()
    other.terminate.assert_not_called()


def test_close_app_not_running(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = SimpleNamespace(process_iter=lambda attrs: [], NoSuchProcess=ProcessLookupError)
    monkeypatch.setitem(sys.modules, "psutil", fake)
    assert "not running" in actions.close_app("paint")


def test_searches_quote_query(no_side_effects: dict[str, MagicMock]) -> None:
    actions.web_search("रेल टिकट & price")
    url = no_side_effects["browser"].call_args.args[0]
    assert url.startswith("https://www.google.com/search?q=")
    assert " " not in url and "&price" not in url
    actions.youtube_search("arijit singh")
    assert no_side_effects["browser"].call_args.args[0].endswith("search_query=arijit+singh")


def test_set_volume_clamps_and_unmutes(fake_volume: MagicMock) -> None:
    assert actions.set_volume(30) == "Volume set to 30%"
    fake_volume.SetMasterVolumeLevelScalar.assert_called_with(0.3, None)
    fake_volume.SetMute.assert_called_with(0, None)
    actions.set_volume(250)
    fake_volume.SetMasterVolumeLevelScalar.assert_called_with(1.0, None)


def test_change_volume(fake_volume: MagicMock) -> None:
    assert actions.change_volume("up", 20) == "Volume is now 70%"
    assert actions.change_volume("down") == "Volume is now 40%"
    assert actions.change_volume("mute") == "Volume muted"
    fake_volume.SetMute.assert_called_with(1, None)


def test_type_text_pastes_and_restores_clipboard(
    fake_pyautogui: MagicMock, fake_pyperclip: MagicMock
) -> None:
    assert actions.type_text("कल मीटिंग है") == "Typed 12 characters"
    fake_pyautogui.hotkey.assert_called_once_with("ctrl", "v")
    fake_pyperclip.copy.assert_any_call("कल मीटिंग है")
    assert fake_pyperclip.store["text"] == "old clipboard"


def test_press_shortcut_and_scroll(fake_pyautogui: MagicMock) -> None:
    actions.press_shortcut("save")
    fake_pyautogui.hotkey.assert_called_with("ctrl", "s")
    actions.scroll("down", "small")
    fake_pyautogui.scroll.assert_called_with(-360)
    actions.scroll("up")
    fake_pyautogui.scroll.assert_called_with(960)


def test_take_screenshot(fake_pyautogui: MagicMock, settings: Settings) -> None:
    result = actions.take_screenshot()
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
    status = actions.system_status()
    assert status["battery_percent"] == 82
    assert status["charging"] is False
    assert status["disk_free_gb"] == 50.0


def test_system_status_without_battery(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = SimpleNamespace(
        sensors_battery=lambda: None,
        disk_usage=lambda p: SimpleNamespace(free=1e9, percent=1),
        cpu_percent=lambda interval: 1,
        virtual_memory=lambda: SimpleNamespace(percent=1),
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)
    assert actions.system_status()["battery_percent"] is None


def test_lock_and_shutdown_use_fixed_commands(no_side_effects: dict[str, MagicMock]) -> None:
    actions.lock_pc()
    no_side_effects["lock"].assert_called_once()
    msg = actions.shutdown_pc()
    cmd = no_side_effects["run"].call_args.args[0]
    assert cmd[0].lower().endswith("shutdown.exe")
    assert cmd[1:] == ["/s", "/t", "60"]
    assert "shell" not in no_side_effects["run"].call_args.kwargs
    assert "cancel" in msg.lower()


def test_cancel_shutdown(no_side_effects: dict[str, MagicMock]) -> None:
    no_side_effects["run"].return_value = subprocess.CompletedProcess([], 0)
    assert actions.cancel_shutdown() == "Shutdown cancelled"
    no_side_effects["run"].return_value = subprocess.CompletedProcess([], 1116)
    assert actions.cancel_shutdown() == "No shutdown was scheduled"


def test_save_note_writes_utf8_and_opens(
    no_side_effects: dict[str, MagicMock], settings: Settings
) -> None:
    result = actions.save_note("छुट्टी की अर्ज़ी", "प्रिय महोदय,\nमुझे छुट्टी चाहिए।")
    path = settings.notes_dir.resolve() / "छुट्टी की अर्ज़ी.txt"
    assert path.read_text(encoding="utf-8").startswith("प्रिय महोदय")
    assert str(path) in result
    no_side_effects["startfile"].assert_called_once_with(str(path))
    # Same title again must not overwrite the first note.
    actions.save_note("छुट्टी की अर्ज़ी", "second")
    assert path.read_text(encoding="utf-8").startswith("प्रिय")
    assert len(list(settings.notes_dir.glob("*.txt"))) == 2


def test_save_note_cannot_escape_folder(
    no_side_effects: dict[str, MagicMock], settings: Settings
) -> None:
    actions.save_note("..\\..\\Windows\\evil", "x")
    files = list(settings.notes_dir.resolve().iterdir())
    assert len(files) == 1
    assert files[0].parent == settings.notes_dir.resolve()


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
    assert len(sanitize_filename("x" * 500)) <= actions.MAX_TITLE_CHARS


def test_current_time() -> None:
    now = actions.current_time()
    assert set(now) == {"time", "date"}

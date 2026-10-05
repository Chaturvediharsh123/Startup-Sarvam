"""Shared fixtures.

Nothing here touches the real PC: Windows-only libraries are replaced with mocks,
and network calls are never made.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from assistant.config import Settings, get_settings, load_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """Make sure no test sees settings cached by another test."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings with a fake key and every path inside ``tmp_path``."""
    return load_settings(
        env_file=None,
        overrides={
            "SARVAM_API_KEY": "sk_test_key",
            "DB_PATH": ":memory:",
            "LOG_DIR": str(tmp_path / "logs"),
            "NOTES_DIR": str(tmp_path / "notes"),
            "SHUTDOWN_DELAY_S": "60",
            "MAX_ACTIONS_PER_MINUTE": "100",
        },
    )


@pytest.fixture
def fake_pyautogui(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace ``pyautogui`` so no key is pressed and no mouse moves."""
    fake = MagicMock(name="pyautogui")
    monkeypatch.setitem(sys.modules, "pyautogui", fake)
    return fake


@pytest.fixture
def fake_pyperclip(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace ``pyperclip`` with an in-memory clipboard."""
    fake = MagicMock(name="pyperclip")
    store = {"text": "old clipboard"}
    fake.paste.side_effect = lambda: store["text"]
    fake.copy.side_effect = lambda value: store.__setitem__("text", value)
    fake.store = store
    fake.PyperclipException = RuntimeError
    monkeypatch.setitem(sys.modules, "pyperclip", fake)
    return fake


@pytest.fixture
def fake_volume(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace pycaw with a fake endpoint volume object (new pycaw API shape)."""
    endpoint = MagicMock(name="EndpointVolume")
    endpoint.GetMasterVolumeLevelScalar.return_value = 0.5
    speakers = MagicMock(name="speakers")
    speakers.EndpointVolume = endpoint
    pycaw_mod = types.ModuleType("pycaw")
    pycaw_pycaw = types.ModuleType("pycaw.pycaw")
    pycaw_pycaw.AudioUtilities = MagicMock(name="AudioUtilities")  # type: ignore[attr-defined]
    pycaw_pycaw.AudioUtilities.GetSpeakers.return_value = speakers  # type: ignore[attr-defined]
    pycaw_pycaw.IAudioEndpointVolume = MagicMock(name="IAudioEndpointVolume")  # type: ignore[attr-defined]
    pycaw_mod.pycaw = pycaw_pycaw  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pycaw", pycaw_mod)
    monkeypatch.setitem(sys.modules, "pycaw.pycaw", pycaw_pycaw)
    monkeypatch.setitem(sys.modules, "comtypes", MagicMock(name="comtypes"))
    return endpoint


@pytest.fixture
def no_side_effects(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    """Block every call that could change the PC: processes, browser, files opening."""
    import assistant.actions as actions

    mocks = {
        "popen": MagicMock(name="Popen"),
        "run": MagicMock(name="run"),
        "startfile": MagicMock(name="startfile"),
        "browser": MagicMock(name="webbrowser.open"),
        "lock": MagicMock(name="lock_workstation"),
    }
    monkeypatch.setattr(actions.subprocess, "Popen", mocks["popen"])
    monkeypatch.setattr(actions.subprocess, "run", mocks["run"])
    monkeypatch.setattr(actions, "_startfile", mocks["startfile"])
    monkeypatch.setattr(actions.webbrowser, "open", mocks["browser"])
    monkeypatch.setattr(actions, "_lock_workstation", mocks["lock"])
    return mocks

"""App-name resolution, Start Menu scan, open and close. Nothing is ever launched."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from actions import apps
from actions.apps import close_app, open_app, resolve_app, scan_start_menu
from actions.registry import NotAllowedError, NotInstalledError


@pytest.mark.parametrize(
    "spoken, key",
    [
        ("chrome", "chrome"),
        ("Chrome", "chrome"),
        ("  GOOGLE   chrome ", "chrome"),
        ("crome", "chrome"),
        ("kroom", "chrome"),
        ("chromee", "chrome"),
        ("क्रोम", "chrome"),
        ("குரோம்", "chrome"),
        ("notepad", "notepad"),
        ("notpad", "notepad"),
        ("नोटपैड", "notepad"),
        ("नोटपेड", "notepad"),
        ("notepad.exe", "notepad"),
        ("Notepad app", "notepad"),
        ("calculater", "calculator"),
        ("calc", "calculator"),
        ("exel", "excel"),
        ("ms word", "word"),
        ("power point", "powerpoint"),
        ("file explorer", "file_manager"),
        ("file_manager", "file_manager"),
        ("कैमरा", "camera"),
        ("photo shop", "photoshop"),
    ],
)
def test_resolve_app_aliases_and_fuzzy(spoken: str, key: str) -> None:
    assert resolve_app(spoken) == key


@pytest.mark.parametrize(
    "spoken",
    [
        "cmd", "cmd.exe", "powershell", "powershel", "pwsh", "regedit", "registry editor",
        "command promt", "terminal", "task manager", "कमांड प्रॉम्प्ट",
        r"C:\Windows\System32\cmd.exe", "../../evil.exe", "%COMSPEC%", "http://x.example/a.exe",
        "notepad && shutdown", "wordpad", "firefox", "", "   ", "ab",
    ],
)
def test_resolve_app_refuses(spoken: str) -> None:
    assert resolve_app(spoken) is None


def test_resolve_app_non_string() -> None:
    assert resolve_app(None) is None  # type: ignore[arg-type]
    assert resolve_app(42) is None  # type: ignore[arg-type]


def _make_start_menu(root: Path, names: list[str]) -> Path:
    folder = root / "Programs"
    (folder / "Sub").mkdir(parents=True)
    for i, name in enumerate(names):
        target = folder / ("Sub" if i % 2 else "") / f"{name}.lnk"
        target.write_bytes(b"")
    (folder / "readme.txt").write_text("not a shortcut")
    return folder


def test_start_menu_scan_and_launch(tmp_path: Path, no_side_effects: dict[str, MagicMock]) -> None:
    folder = _make_start_menu(tmp_path, ["Google Chrome", "Word", "Adobe Photoshop 2024"])
    index = scan_start_menu([folder, tmp_path / "missing"])
    assert set(index) == {"google chrome", "word", "adobe photoshop 2024"}
    assert open_app("crome") == "opened chrome"
    no_side_effects["startfile"].assert_called_with(str(folder / "Google Chrome.lnk"))
    assert open_app("photo shop") == "opened photoshop"
    assert no_side_effects["startfile"].call_args.args[0].endswith("Adobe Photoshop 2024.lnk")
    no_side_effects["popen"].assert_not_called()


def test_shortcuts_scans_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    folder = _make_start_menu(tmp_path, ["Google Chrome"])
    monkeypatch.setattr(apps, "_shortcuts", None)
    monkeypatch.setattr(apps, "start_menu_dirs", lambda: [folder])
    calls: list[int] = []
    real = apps.scan_start_menu
    monkeypatch.setattr(apps, "scan_start_menu", lambda dirs=None: calls.append(1) or real(dirs))
    apps.shortcuts()
    apps.shortcuts()
    assert calls == [1]


def test_builtin_app_opens_fixed_target(no_side_effects: dict[str, MagicMock]) -> None:
    assert open_app("नोटपैड") == "opened notepad"
    no_side_effects["startfile"].assert_called_once_with("notepad.exe")


def test_not_installed_gives_friendly_fact(no_side_effects: dict[str, MagicMock]) -> None:
    with pytest.raises(NotInstalledError, match="^photoshop not installed$"):
        open_app("photoshop")
    no_side_effects["startfile"].assert_not_called()


def test_app_paths_registry_counts_as_installed(
    no_side_effects: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(apps, "_app_path_registered", lambda exe: exe == "chrome.exe")
    open_app("chrome")
    no_side_effects["startfile"].assert_called_once_with("chrome.exe")


@pytest.mark.parametrize("name", ["cmd", r"C:\Windows\System32\cmd.exe", "regedit", "firefox"])
def test_open_app_not_allowed(name: str, no_side_effects: dict[str, MagicMock]) -> None:
    with pytest.raises(NotAllowedError, match="not an allowed app"):
        open_app(name)
    no_side_effects["startfile"].assert_not_called()


def _fake_psutil(procs: list[MagicMock], alive: list[MagicMock]) -> SimpleNamespace:
    return SimpleNamespace(
        process_iter=lambda attrs: procs,
        wait_procs=MagicMock(return_value=([], alive)),
        NoSuchProcess=ProcessLookupError,
    )


def test_close_app_terminates_then_kills(monkeypatch: pytest.MonkeyPatch) -> None:
    chrome = MagicMock(info={"name": "chrome.exe"})
    stubborn = MagicMock(info={"name": "Chrome.exe"})
    other = MagicMock(info={"name": "notepad.exe"})
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil([chrome, stubborn, other], [stubborn]))
    assert close_app("kroom") == "closed chrome"
    chrome.terminate.assert_called_once()
    stubborn.kill.assert_called_once()
    other.terminate.assert_not_called()


def test_close_app_not_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil([], []))
    assert close_app("paint") == "paint is not running"


def test_close_app_refuses_explorer_and_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    explorer = MagicMock(info={"name": "explorer.exe"})
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil([explorer], []))
    with pytest.raises(NotAllowedError, match="cannot be closed"):
        close_app("file manager")
    with pytest.raises(NotAllowedError):
        close_app("svchost")
    explorer.terminate.assert_not_called()

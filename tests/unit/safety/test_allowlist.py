"""The allow-list file itself, and that Safety and Actions resolve app names the same way."""

from __future__ import annotations

import pytest

from actions.apps import resolve_app as actions_resolve
from core.config import ALLOWLIST_FILE
from safety.allowlist import Allowlist, split_combo


@pytest.fixture(scope="module")
def allow() -> Allowlist:
    return Allowlist.load(ALLOWLIST_FILE)


def test_every_action_has_risk(allow: Allowlist) -> None:
    assert len(allow.actions) == 16
    assert {r.risk for r in allow.actions.values()} == {"low", "medium", "high"}
    assert allow.risk("not_there") == "high"


def test_no_safe_shortcut_is_blocked(allow: Allowlist) -> None:
    for name, keys in allow.safe_shortcuts.items():
        assert not allow.is_blocked_combo(keys), name


def test_blocked_apps_never_aliases(allow: Allowlist) -> None:
    for blocked in allow.raw["blocked_apps"]:
        assert allow.resolve_app(blocked) is None, blocked


@pytest.mark.parametrize(
    "text, keys",
    [("Alt+F4", {"alt", "f4"}), ("ctrl + alt + del", {"ctrl", "alt", "delete"}),
     ("windows r", {"win", "r"}), ("Control plus S", {"ctrl", "s"})],
)
def test_split_combo(text: str, keys: set[str]) -> None:
    assert split_combo(text) == keys


@pytest.mark.parametrize(
    "name",
    ["chrome", "crome", "kroom", "क्रोम", "notpad", "नोटपैड", "नोटपेड", "calculater", "exel",
     "cmd", "powershell", "regedit", r"C:\Windows\System32\cmd.exe", "wordpad", "photo shop",
     "file explorer", "command promt", "notepad.exe", "ms word", "power pint", "random thing"],
)
def test_safety_and_actions_agree(allow: Allowlist, name: str) -> None:
    assert allow.resolve_app(name) == actions_resolve(name)


def test_bad_risk_rejected(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from core.config import ConfigError

    path = tmp_path / "a.yaml"
    path.write_text("actions:\n  x: {risk: extreme}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        Allowlist.load(path)

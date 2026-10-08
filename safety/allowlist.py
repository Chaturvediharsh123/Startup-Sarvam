"""The allow-list file ``config/allowlist.yaml``, as Safety reads it.

Safety owns this file: one entry per action with its risk level and allowed
arguments, the allowed apps with their aliases, the safe shortcuts and the
blocked key combos. Safety never imports ``actions``, so app-name resolution is
done here too (the same rules as ``actions.apps.resolve_app``; a test keeps the
two in step).
"""

from __future__ import annotations

import difflib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.config import ALLOWLIST_FILE, ConfigError

RISK_LEVELS = ("low", "medium", "high")
FUZZY_CUTOFF = 0.8
BLOCKED_FUZZY_CUTOFF = 0.85
MIN_FUZZY_CHARS = 4
_FILLER_WORDS = frozenset({"app", "application", "the", "program", "software", "ऐप", "एप"})
_PATH_CHARS = ("\\", "/", ":", "..", "%", "*", "?", "|", "<", ">", '"')


def normalise_name(text: str) -> str:
    """Lower-case, NFC, punctuation and filler words removed, spaces collapsed."""
    norm = unicodedata.normalize("NFC", text).casefold().strip()
    if norm.endswith(".exe"):
        norm = norm[:-4]
    cleaned = "".join(
        " " if unicodedata.category(c)[0] in ("P", "S", "Z") or c in "_-." else c
        for c in norm
        if unicodedata.category(c) != "Cf"
    )
    words = cleaned.split()
    kept = [w for w in words if w not in _FILLER_WORDS]
    return " ".join(kept or words)


def _squash(text: str) -> str:
    return normalise_name(text).replace(" ", "")


_KEY_NAMES = {
    "control": "ctrl", "windows": "win", "window": "win", "super": "win", "meta": "win",
    "escape": "esc", "return": "enter", "option": "alt", "delete": "delete", "del": "delete",
}


def split_combo(text: str) -> frozenset[str]:
    """``"Ctrl + Alt + Del"`` -> ``{"ctrl", "alt", "delete"}``."""
    norm = unicodedata.normalize("NFKC", text).casefold().replace(" plus ", "+")
    parts = [p.strip() for p in norm.replace(",", "+").split("+")]
    if len(parts) == 1:
        parts = norm.split()
    return frozenset(_KEY_NAMES.get(p, p) for p in parts if p)


@dataclass(frozen=True)
class ActionRule:
    """One action's entry: risk level, informational flag and argument rules."""

    name: str
    risk: str = "low"
    informational: bool = False
    args: dict[str, dict[str, Any]] = field(default_factory=dict)


class Allowlist:
    """Parsed ``config/allowlist.yaml`` with the lookups the guard needs."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.raw = data
        self.actions: dict[str, ActionRule] = {}
        for name, entry in (data.get("actions") or {}).items():
            entry = entry or {}
            risk = str(entry.get("risk", "low")).lower()
            if risk not in RISK_LEVELS:
                raise ConfigError(f"allowlist: {name} has unknown risk {risk!r}")
            self.actions[str(name)] = ActionRule(
                str(name), risk, bool(entry.get("informational", False)),
                {str(k): dict(v or {}) for k, v in (entry.get("args") or {}).items()},
            )
        self.apps: dict[str, dict[str, Any]] = {
            str(k): dict(v or {}) for k, v in (data.get("apps") or {}).items()
        }
        self.safe_shortcuts: dict[str, frozenset[str]] = {
            str(k).lower(): frozenset(str(x).lower() for x in v)
            for k, v in (data.get("safe_shortcuts") or {}).items()
        }
        self.blocked_combos: list[frozenset[str]] = [
            frozenset(str(x).lower() for x in c) for c in data.get("blocked_key_combos") or ()
        ]
        self.blocked_keys = frozenset(str(k).lower() for k in data.get("blocked_keys") or ())
        self.limits: dict[str, int] = {
            str(k): int(v) for k, v in (data.get("limits") or {}).items()
        }
        self._blocked_apps = {_squash(str(b)) for b in data.get("blocked_apps") or ()}
        self._aliases: dict[str, str] = {}
        for key, app in self.apps.items():
            for alias in (key, *(app.get("aliases") or ())):
                self._aliases.setdefault(_squash(str(alias)), key)

    @classmethod
    def load(cls, path: Path | None = None) -> Allowlist:
        """Read the YAML file (default ``config/allowlist.yaml``)."""
        import yaml

        target = path or ALLOWLIST_FILE
        data: Any = yaml.safe_load(Path(target).read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ConfigError(f"{Path(target).name} must be a mapping")
        return cls(data)

    def risk(self, name: str) -> str:
        """The action's risk level (``high`` for anything unknown)."""
        rule = self.actions.get(name)
        return rule.risk if rule else "high"

    def limit(self, name: str, default: int) -> int:
        """A character limit from the ``limits`` section."""
        return self.limits.get(name, default)

    # -- apps ----------------------------------------------------------------------

    def resolve_app(self, name: Any) -> str | None:
        """The allow-listed app key for a spoken/typed name, or ``None``.

        ``None`` for paths, blocked apps (cmd, powershell, ...) and unknown names.
        """
        if not isinstance(name, str) or not name.strip() or any(c in name for c in _PATH_CHARS):
            return None
        squashed = _squash(name)
        if not squashed or squashed in self._blocked_apps:
            return None
        if squashed in self._aliases:
            return self._aliases[squashed]
        if len(squashed) < MIN_FUZZY_CHARS:
            return None
        if difflib.get_close_matches(squashed, self._blocked_apps, 1, BLOCKED_FUZZY_CUTOFF):
            return None
        match = difflib.get_close_matches(squashed, self._aliases, 1, FUZZY_CUTOFF)
        return self._aliases[match[0]] if match else None

    def closable(self, key: str) -> bool:
        """True when ``close_app`` may end this app (it lists process names)."""
        return bool(self.apps.get(key, {}).get("processes"))

    # -- keys ----------------------------------------------------------------------

    def is_blocked_combo(self, keys: frozenset[str]) -> bool:
        """True for a blocked combo, or any combo containing a blocked key."""
        return bool(keys & self.blocked_keys) or keys in self.blocked_combos

    def shortcut(self, value: str) -> str:
        """The safe shortcut name for a name or key combo.

        Raises:
            ValueError: blocked combo or not on the safe list (message says which).
        """
        text = " ".join(str(value).strip().casefold().split())
        name = text.replace(" ", "_")
        if name in self.safe_shortcuts:
            if self.is_blocked_combo(self.safe_shortcuts[name]):
                raise ValueError(f"{value} is a blocked key combination")
            return name
        keys = split_combo(text)
        if self.is_blocked_combo(keys):
            raise ValueError(f"{value} is a blocked key combination")
        for safe, combo in self.safe_shortcuts.items():
            if keys == combo:
                return safe
        raise ValueError(f"{value} is not on the safe shortcut list")

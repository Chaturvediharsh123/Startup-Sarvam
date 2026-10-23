"""The one registry of PC actions (PDF "Actions": named functions in one registry).

Each action is a plain function registered with :func:`action`: a name, a plain
description, its argument list (JSON schema), a risk level and the function. The
LLM tool list is generated from this registry, so adding an action is one function
plus one decorator line (and one entry in ``config/allowlist.yaml``).

Rules for every action:

* Never run a shell, and never build a command or a path from LLM input.
* App names, shortcut keys and text limits come from ``config/allowlist.yaml``.
* Windows-only libraries are imported inside the function that needs them.
* Return a short English fact (``"opened chrome"``), or a dict for informational
  actions. Raise :class:`ActionError` with a friendly fact when something is wrong.

The small wrappers at the bottom (``_startfile``, ``_lock_workstation``) are the
only places that touch the PC directly, so tests can patch them in one spot.
"""

from __future__ import annotations

import logging
import os
import subprocess  # noqa: F401 - patched by tests/conftest.py (no_side_effects)
import threading
import time
import webbrowser  # noqa: F401 - patched by tests/conftest.py (no_side_effects)
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.config import ALLOWLIST_FILE, ConfigError, Settings, get_settings, load_settings

logger = logging.getLogger(__name__)

ActionFunc = Callable[..., Any]
FactFunc = Callable[[Any], str]
RISK_LEVELS = ("low", "medium", "high")


class ActionError(RuntimeError):
    """A friendly, expected failure. ``str(exc)`` is the short English fact."""


class NotAllowedError(ActionError):
    """The request names something that is not on the allow-list."""


class NotInstalledError(ActionError):
    """An allow-listed app that is not installed on this PC."""


@dataclass(frozen=True)
class ActionSpec:
    """A registered action and its metadata."""

    name: str
    func: ActionFunc
    description: str
    parameters: dict[str, Any]
    risk: str = "low"
    informational: bool = False
    fact: FactFunc | None = None

    @property
    def risky(self) -> bool:
        """True for high-risk actions, which need a spoken yes."""
        return self.risk == "high"

    def tool_schema(self) -> dict[str, Any]:
        """OpenAI-style function tool definition for this action."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": _describe(self.description),
                "parameters": _resolve_parameters(self.parameters),
            },
        }

    def to_fact(self, result: Any) -> str:
        """The short English fact for a successful result."""
        if self.fact is not None:
            return self.fact(result)
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            return ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in result.items())
        return f"{self.name.replace('_', ' ')} done"


ACTIONS: dict[str, ActionSpec] = {}


def action(
    description: str,
    params: dict[str, dict[str, Any]] | None = None,
    required: Sequence[str] = (),
    risk: str = "low",
    informational: bool = False,
    fact: FactFunc | None = None,
) -> Callable[[ActionFunc], ActionFunc]:
    """Register a function as an action.

    Args:
        description: what the action does, written for the LLM. ``{apps}`` and
            ``{shortcuts}`` are filled from the allow-list.
        params: JSON-schema properties, keyed by argument name. ``"x-enum":
            "safe_shortcuts"`` becomes an ``enum`` of the allow-listed shortcut names.
        required: argument names the LLM must always send.
        risk: ``low`` | ``medium`` | ``high`` (must match ``config/allowlist.yaml``).
        informational: returns data the LLM should turn into a spoken answer.
        fact: turns the result into a short English fact (default: the string itself).
    """
    if risk not in RISK_LEVELS:
        raise ValueError(f"risk must be one of {RISK_LEVELS}, got {risk!r}")

    def register(func: ActionFunc) -> ActionFunc:
        """Record ``func`` in :data:`ACTIONS` with its tool schema."""
        properties = params or {}
        unknown = set(required) - set(properties)
        if unknown:
            raise ValueError(f"{func.__name__}: required params not defined: {unknown}")
        parameters = {
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        }
        ACTIONS[func.__name__] = ActionSpec(
            func.__name__, func, description, parameters, risk, informational, fact
        )
        return func

    return register


def tool_schemas() -> list[dict[str, Any]]:
    """Tool definitions for every registered action, for the LLM ``tools`` field."""
    return [spec.tool_schema() for spec in ACTIONS.values()]


# ---------------------------------------------------------------------------
# Settings and the allow-list file
# ---------------------------------------------------------------------------

_settings: Settings | None = None
_allowlist: dict[str, Any] | None = None
_allowlist_path: Path = ALLOWLIST_FILE
_lock = threading.Lock()
_sleep = time.sleep


def configure(settings: Settings) -> None:
    """Give the actions their settings (notes folder, shutdown delay, allow-list path)."""
    global _settings, _allowlist, _allowlist_path
    with _lock:
        _settings = settings
        _allowlist_path = settings.allowlist_path
        _allowlist = None


def config() -> Settings:
    """The settings given to :func:`configure`, or the process-wide ones."""
    if _settings is not None:
        return _settings
    try:
        return get_settings()
    except ConfigError:
        return load_settings(require_key=False)


def load_allowlist(path: Path) -> dict[str, Any]:
    """Read ``config/allowlist.yaml`` into a dict (empty sections when missing)."""
    import yaml

    data: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must be a mapping")
    for section in ("actions", "apps", "safe_shortcuts", "limits"):
        data[section] = data.get(section) or {}
    for section in ("blocked_apps", "blocked_key_combos", "blocked_keys"):
        data[section] = data.get(section) or []
    return data


def allowlist() -> dict[str, Any]:
    """The allow-list, loaded once (and again after :func:`configure`)."""
    global _allowlist
    with _lock:
        if _allowlist is None:
            _allowlist = load_allowlist(_allowlist_path)
        return _allowlist


def limit(name: str, default: int) -> int:
    """A character limit from the allow-list ``limits`` section."""
    return int(allowlist()["limits"].get(name, default))


def safe_shortcuts() -> dict[str, tuple[str, ...]]:
    """Allow-listed shortcut name -> keys pressed together."""
    return {
        str(k): tuple(str(x).lower() for x in v)
        for k, v in allowlist()["safe_shortcuts"].items()
    }


def _describe(text: str) -> str:
    """Fill ``{apps}`` / ``{shortcuts}`` placeholders in a description."""
    if "{" not in text:
        return text
    data = allowlist()
    return text.format(apps=", ".join(data["apps"]), shortcuts=", ".join(data["safe_shortcuts"]))


def _resolve_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    """Copy the schema, turning ``x-enum`` markers into real enums from the allow-list."""
    properties: dict[str, Any] = {}
    for key, schema in parameters["properties"].items():
        prop = {k: v for k, v in schema.items() if not k.startswith("x-")}
        source = schema.get("x-enum")
        if source:
            prop["enum"] = sorted(allowlist()[source])
        properties[key] = prop
    return {**parameters, "properties": properties}


# Back-compat views of the allow-list (used by sarvam/mock.py and older callers).
def _apps_view() -> dict[str, str]:
    return {k: str(v.get("launch", "")) for k, v in allowlist()["apps"].items()}


def _process_view() -> dict[str, tuple[str, ...]]:
    return {
        k: tuple(str(p).lower() for p in v.get("processes") or ())
        for k, v in allowlist()["apps"].items()
        if v.get("processes")
    }


def __getattr__(name: str) -> Any:
    """Lazy ``APPS`` / ``PROCESS_NAMES`` / ``SAFE_HOTKEYS`` built from the allow-list."""
    views: dict[str, Callable[[], Any]] = {
        "APPS": _apps_view,
        "PROCESS_NAMES": _process_view,
        "SAFE_HOTKEYS": safe_shortcuts,
    }
    if name in views:
        return views[name]()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ---------------------------------------------------------------------------
# The only functions that touch the PC directly (patched in tests)
# ---------------------------------------------------------------------------


def _startfile(target: str) -> None:
    """Open a file, program or URI with its default Windows handler."""
    startfile = getattr(os, "startfile", None)
    if startfile is None:
        raise ActionError("opening apps only works on Windows")
    startfile(target)


def _lock_workstation() -> None:
    """Lock the Windows session."""
    import ctypes

    if not ctypes.windll.user32.LockWorkStation():  # type: ignore[attr-defined]
        raise ActionError("could not lock the pc")

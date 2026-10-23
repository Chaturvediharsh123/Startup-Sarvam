"""Open and close allow-listed apps by name (PDF "Actions": apps.py).

The LLM passes the app name as the user said it ("crome", "kroom", "नोटपैड").
:func:`resolve_app` maps it to one allow-listed app key using the aliases in
``config/allowlist.yaml``, case/space normalisation and a careful fuzzy match.
Names on ``blocked_apps`` (cmd, powershell, regedit, ...) never resolve, and
anything that looks like a path is refused outright.

Installed apps are found by scanning the Start Menu ``*.lnk`` shortcuts once.
Only shortcuts whose names match an allow-listed app's ``shortcuts`` patterns are
ever launched, so no arbitrary path can be opened.
"""

from __future__ import annotations

import difflib
import fnmatch
import logging
import os
import threading
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from actions import registry
from actions.registry import NotAllowedError, NotInstalledError, action

logger = logging.getLogger(__name__)

FUZZY_CUTOFF = 0.8
BLOCKED_FUZZY_CUTOFF = 0.85
MIN_FUZZY_CHARS = 4
_FILLER_WORDS = frozenset({"app", "application", "the", "program", "software", "ऐप", "एप"})
_PATH_CHARS = ("\\", "/", ":", "..", "%", "*", "?", "|", "<", ">", '"')


# ---------------------------------------------------------------------------
# Name resolution
# ---------------------------------------------------------------------------


def normalise_name(text: str) -> str:
    """Lower-case, NFC, punctuation and filler words removed, spaces collapsed."""
    norm = unicodedata.normalize("NFC", text).casefold().strip()
    if norm.endswith(".exe"):
        norm = norm[:-4]
    cleaned = "".join(
        " " if unicodedata.category(c)[0] in ("P", "S", "Z") or c in "_-." else c
        for c in norm
        if unicodedata.category(c) != "Cf"  # zero-width joiners in Indic spellings
    )
    words = cleaned.split()
    kept = [w for w in words if w not in _FILLER_WORDS]
    return " ".join(kept or words)


def _squash(text: str) -> str:
    return text.replace(" ", "")


def _alias_index() -> dict[str, str]:
    """Normalised alias (and app key) -> app key, spaces removed."""
    index: dict[str, str] = {}
    for key, app in registry.allowlist()["apps"].items():
        for alias in (key, *(app.get("aliases") or ())):
            index.setdefault(_squash(normalise_name(str(alias))), str(key))
    return index


def _blocked_names() -> set[str]:
    return {_squash(normalise_name(str(b))) for b in registry.allowlist()["blocked_apps"]}


def resolve_app(name: str) -> str | None:
    """The allow-listed app key for a spoken/typed app name, or ``None``.

    ``None`` for paths, blocked apps (cmd, powershell, ...) and unknown names.
    """
    if not isinstance(name, str) or not name.strip() or any(c in name for c in _PATH_CHARS):
        return None
    squashed = _squash(normalise_name(name))
    if not squashed:
        return None
    blocked = _blocked_names()
    if squashed in blocked:
        return None
    index = _alias_index()
    if squashed in index:
        return index[squashed]
    if len(squashed) < MIN_FUZZY_CHARS:
        return None
    if difflib.get_close_matches(squashed, blocked, n=1, cutoff=BLOCKED_FUZZY_CUTOFF):
        return None
    match = difflib.get_close_matches(squashed, index, n=1, cutoff=FUZZY_CUTOFF)
    return index[match[0]] if match else None


def _app(key: str) -> dict[str, Any]:
    return dict(registry.allowlist()["apps"][key])


def _label(key: str) -> str:
    return key.replace("_", " ")


# ---------------------------------------------------------------------------
# Start Menu scan (once per launch)
# ---------------------------------------------------------------------------

_shortcuts: dict[str, Path] | None = None
_scan_lock = threading.Lock()


def start_menu_dirs() -> list[Path]:
    """The machine-wide and per-user Start Menu ``Programs`` folders."""
    dirs: list[Path] = []
    for env in ("ProgramData", "APPDATA"):
        base = os.environ.get(env)
        if base:
            dirs.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return dirs


def scan_start_menu(dirs: Iterable[Path] | None = None) -> dict[str, Path]:
    """Index every Start Menu ``*.lnk`` by its lower-case name. Replaces the old index."""
    global _shortcuts
    index: dict[str, Path] = {}
    for folder in start_menu_dirs() if dirs is None else dirs:
        try:
            for link in sorted(Path(folder).rglob("*.lnk")):
                index.setdefault(" ".join(link.stem.casefold().split()), link)
        except OSError:
            logger.warning("Could not scan Start Menu folder %s", folder)
    with _scan_lock:
        _shortcuts = index
    logger.info("Start Menu scan found %d shortcuts", len(index))
    return index


def shortcuts() -> dict[str, Path]:
    """The Start Menu index, scanning on first use."""
    with _scan_lock:
        cached = _shortcuts
    return cached if cached is not None else scan_start_menu()


def find_shortcut(key: str) -> Path | None:
    """The Start Menu shortcut that proves an allow-listed app is installed."""
    patterns = [str(p).casefold() for p in _app(key).get("shortcuts") or ()]
    index = shortcuts()
    for pattern in patterns:
        for stem in sorted(index):
            if fnmatch.fnmatchcase(stem, pattern):
                return index[stem]
    return None


def _app_path_registered(exe: str) -> bool:
    """True when Windows "App Paths" knows ``exe`` (how ``os.startfile("chrome")`` works)."""
    try:
        import winreg
    except ImportError:
        return False
    sub = rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, sub):
                return True
        except OSError:
            continue
    return False


def launch_target(key: str) -> str:
    """What to pass to ``os.startfile`` for an allow-listed app.

    Raises:
        NotInstalledError: the app is allow-listed but not on this PC.
    """
    app = _app(key)
    launch = str(app.get("launch") or "")
    if app.get("builtin"):
        return launch
    link = find_shortcut(key)
    if link is not None:
        return str(link)
    if launch and _app_path_registered(launch):
        return launch
    raise NotInstalledError(f"{_label(key)} not installed")


def _require(name: str) -> str:
    key = resolve_app(name)
    if key is None:
        raise NotAllowedError(f"{name.strip()[:40]} is not an allowed app")
    return key


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


@action(
    "Open an application on the PC. Pass the app name as the user said it. "
    "Allowed apps: {apps}.",
    {"name": {"type": "string", "minLength": 1, "maxLength": 60, "description": "App name"}},
    required=["name"],
    risk="low",
)
def open_app(name: str) -> str:
    """Launch an allow-listed app (through its Start Menu shortcut when installed)."""
    key = _require(name)
    registry._startfile(launch_target(key))
    return f"opened {_label(key)}"


@action(
    "Close an open application. Asks the user to confirm first. Allowed apps: {apps}.",
    {"name": {"type": "string", "minLength": 1, "maxLength": 60, "description": "App name"}},
    required=["name"],
    risk="high",
)
def close_app(name: str) -> str:
    """End an allow-listed app's processes, killing any that refuse."""
    import psutil

    key = _require(name)
    targets = {str(p).lower() for p in _app(key).get("processes") or ()}
    if not targets:
        raise NotAllowedError(f"{_label(key)} cannot be closed")
    procs = [
        p for p in psutil.process_iter(["name"]) if (p.info.get("name") or "").lower() in targets
    ]
    if not procs:
        return f"{_label(key)} is not running"
    for proc in procs:
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            continue
    _, alive = psutil.wait_procs(procs, timeout=3)
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            continue
    return f"closed {_label(key)}"

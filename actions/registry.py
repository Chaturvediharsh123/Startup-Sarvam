"""The allow-listed PC actions the assistant may perform.

Each action is a plain function registered with :func:`action`. The decorator
records it in :data:`ACTIONS` and builds the OpenAI-style tool schema that the
Sarvam Chat Completions API accepts (see docs/sarvam_api_notes.md, section 3).

Rules for every action:

* Never run a shell, and never build a command string from LLM input.
* Fixed choices are JSON-schema ``enum`` values, so the model cannot invent them.
* Windows-only libraries are imported inside the function that needs them.
* Return a short English result string, or a small dict for informational actions.

Actions are only ever called through :func:`safety.guard.execute`.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import time
import unicodedata
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from core.config import Settings, get_settings

ActionFunc = Callable[..., Any]


@dataclass(frozen=True)
class ActionSpec:
    """A registered action and its metadata."""

    name: str
    func: ActionFunc
    description: str
    parameters: dict[str, Any]
    risky: bool = False
    informational: bool = False

    def tool_schema(self) -> dict[str, Any]:
        """OpenAI-style function tool definition for this action."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


ACTIONS: dict[str, ActionSpec] = {}

_settings: Settings | None = None
_sleep = time.sleep


def configure(settings: Settings) -> None:
    """Give the actions their settings (notes folder, shutdown delay)."""
    global _settings
    _settings = settings


def _config() -> Settings:
    return _settings if _settings is not None else get_settings()


def action(
    description: str,
    params: dict[str, dict[str, Any]] | None = None,
    required: Sequence[str] = (),
    risky: bool = False,
    informational: bool = False,
) -> Callable[[ActionFunc], ActionFunc]:
    """Register a function as an allow-listed action.

    Args:
        description: what the action does, written for the LLM.
        params: JSON-schema properties, keyed by argument name.
        required: argument names the LLM must always send.
        risky: needs a spoken "yes" before it runs.
        informational: returns data the LLM should turn into a spoken answer.
    """

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
            func.__name__, func, description, parameters, risky, informational
        )
        return func

    return register


def tool_schemas() -> list[dict[str, Any]]:
    """Tool definitions for every registered action, for the LLM ``tools`` field."""
    return [spec.tool_schema() for spec in ACTIONS.values()]


# ---------------------------------------------------------------------------
# Allow-lists
# ---------------------------------------------------------------------------

# App name -> target passed to os.startfile (an executable on PATH / App Paths, or a URI).
APPS: dict[str, str] = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "paint": "mspaint.exe",
    "file_manager": "explorer.exe",
    "chrome": "chrome",
    "edge": "msedge",
    "word": "winword",
    "excel": "excel",
    "powerpoint": "powerpnt",
    "settings": "ms-settings:",
    "camera": "microsoft.windows.camera:",
}

# App name -> process names that close_app may terminate. File manager (explorer.exe)
# is left out on purpose: killing it would take down the taskbar.
PROCESS_NAMES: dict[str, tuple[str, ...]] = {
    "notepad": ("notepad.exe",),
    "calculator": ("calculatorapp.exe", "calculator.exe"),
    "paint": ("mspaint.exe",),
    "chrome": ("chrome.exe",),
    "edge": ("msedge.exe",),
    "word": ("winword.exe",),
    "excel": ("excel.exe",),
    "powerpoint": ("powerpnt.exe",),
    "settings": ("systemsettings.exe",),
    "camera": ("windowscamera.exe",),
}

SAFE_HOTKEYS: dict[str, tuple[str, ...]] = {
    "save": ("ctrl", "s"),
    "copy": ("ctrl", "c"),
    "paste": ("ctrl", "v"),
    "undo": ("ctrl", "z"),
    "redo": ("ctrl", "y"),
    "select_all": ("ctrl", "a"),
    "new_tab": ("ctrl", "t"),
    "close_tab": ("ctrl", "w"),
    "refresh": ("f5",),
    "enter": ("enter",),
    "minimize_all": ("win", "d"),
    "switch_window": ("alt", "tab"),
    "zoom_in": ("ctrl", "="),
    "zoom_out": ("ctrl", "-"),
}

# Mouse-wheel notches; Windows reports one notch as a delta of 120.
SCROLL_NOTCHES: dict[str, int] = {"small": 3, "medium": 8, "large": 15}
WHEEL_DELTA = 120

MAX_TYPE_CHARS = 2000
MAX_QUERY_CHARS = 200
MAX_NOTE_CHARS = 20000
MAX_TITLE_CHARS = 80
# Full path so a planted shutdown.exe elsewhere on PATH can never be run.
SHUTDOWN_EXE = str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "shutdown.exe")
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


# ---------------------------------------------------------------------------
# Small wrappers (patched in tests so nothing real happens)
# ---------------------------------------------------------------------------


def _startfile(target: str) -> None:
    """Open a file, program or URI with its default Windows handler."""
    startfile = getattr(os, "startfile", None)
    if startfile is None:
        raise RuntimeError("Opening apps is only supported on Windows")
    startfile(target)


def _lock_workstation() -> None:
    """Lock the Windows session."""
    import ctypes

    if not ctypes.windll.user32.LockWorkStation():  # type: ignore[attr-defined]
        raise RuntimeError("LockWorkStation failed")


def _endpoint_volume() -> Any:
    """Return the speaker volume COM object, supporting new and old pycaw APIs."""
    import comtypes
    from pycaw.pycaw import AudioUtilities

    with contextlib.suppress(OSError):
        comtypes.CoInitialize()  # needed in worker threads; harmless if already done
    speakers = AudioUtilities.GetSpeakers()
    endpoint = getattr(speakers, "EndpointVolume", None)
    if endpoint is not None:  # pycaw >= 20240210
        return endpoint
    from ctypes import POINTER, cast

    from pycaw.pycaw import IAudioEndpointVolume

    interface = speakers.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


def sanitize_filename(title: str) -> str:
    """Make a safe Windows file name (without extension) from a note title.

    Removes path separators, ``..``, reserved characters and control characters,
    and avoids reserved device names such as ``CON`` or ``LPT1``.
    """
    text = unicodedata.normalize("NFC", title)
    text = "".join(" " if unicodedata.category(c).startswith("C") else c for c in text)
    text = re.sub(r'[<>:"/\\|?*]', " ", text)
    text = text.replace("..", " ")
    text = " ".join(text.split()).strip(" .")
    text = text[:MAX_TITLE_CHARS].strip(" .")
    if not text:
        return "note"
    if text.split(".")[0].strip().lower() in _RESERVED_NAMES:
        text = f"note_{text}"
    return text


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


@action(
    "Open an application on the PC.",
    {"name": {"type": "string", "enum": sorted(APPS), "description": "Which app to open"}},
    required=["name"],
)
def open_app(name: str) -> str:
    """Launch an allow-listed app."""
    _startfile(APPS[name])
    return f"Opened {name}"


@action(
    "Close an open application. Asks the user to confirm first.",
    {"name": {"type": "string", "enum": sorted(PROCESS_NAMES), "description": "Which app"}},
    required=["name"],
    risky=True,
)
def close_app(name: str) -> str:
    """Terminate an allow-listed app's processes, killing any that refuse."""
    import psutil

    targets = set(PROCESS_NAMES[name])
    procs = [
        p for p in psutil.process_iter(["name"]) if (p.info.get("name") or "").lower() in targets
    ]
    if not procs:
        return f"{name} is not running"
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
    return f"Closed {name}"


@action(
    "Search Google in the web browser.",
    {"query": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARS}},
    required=["query"],
)
def web_search(query: str) -> str:
    """Open Google results for ``query``."""
    webbrowser.open("https://www.google.com/search?q=" + quote_plus(query))
    return f"Searched the web for {query}"


@action(
    "Search YouTube in the web browser, for songs, videos or bhajans.",
    {"query": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARS}},
    required=["query"],
)
def youtube_search(query: str) -> str:
    """Open YouTube results for ``query``."""
    webbrowser.open("https://www.youtube.com/results?search_query=" + quote_plus(query))
    return f"Searched YouTube for {query}"


@action(
    "Set the speaker volume to an exact percentage.",
    {"level": {"type": "integer", "minimum": 0, "maximum": 100, "description": "0 to 100"}},
    required=["level"],
)
def set_volume(level: int) -> str:
    """Set the master volume (0–100)."""
    level = max(0, min(100, int(level)))
    endpoint = _endpoint_volume()
    endpoint.SetMasterVolumeLevelScalar(level / 100, None)
    if level > 0:
        endpoint.SetMute(0, None)
    return f"Volume set to {level}%"


@action(
    "Turn the volume up or down, or mute/unmute it.",
    {
        "direction": {"type": "string", "enum": ["up", "down", "mute", "unmute"]},
        "step": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
    },
    required=["direction"],
)
def change_volume(direction: str, step: int = 10) -> str:
    """Change the master volume relative to its current level."""
    endpoint = _endpoint_volume()
    if direction == "mute":
        endpoint.SetMute(1, None)
        return "Volume muted"
    if direction == "unmute":
        endpoint.SetMute(0, None)
        return "Volume unmuted"
    current = round(endpoint.GetMasterVolumeLevelScalar() * 100)
    delta = step if direction == "up" else -step
    level = max(0, min(100, current + delta))
    endpoint.SetMasterVolumeLevelScalar(level / 100, None)
    endpoint.SetMute(0, None)
    return f"Volume is now {level}%"


@action(
    "Type text into the window that is open now. Works for every language and script.",
    {"text": {"type": "string", "minLength": 1, "maxLength": MAX_TYPE_CHARS}},
    required=["text"],
)
def type_text(text: str) -> str:
    """Paste ``text`` via the clipboard (handles Hindi, Tamil, …) and restore the clipboard."""
    import pyautogui
    import pyperclip

    try:
        previous = pyperclip.paste()
    except pyperclip.PyperclipException:
        previous = None
    pyperclip.copy(text)
    try:
        _sleep(0.05)
        pyautogui.hotkey("ctrl", "v")
        _sleep(0.3)
    finally:
        if previous is not None:
            pyperclip.copy(previous)
    return f"Typed {len(text)} characters"


@action(
    "Press a common keyboard shortcut in the current window.",
    {"shortcut": {"type": "string", "enum": sorted(SAFE_HOTKEYS)}},
    required=["shortcut"],
)
def press_shortcut(shortcut: str) -> str:
    """Press one allow-listed key combination."""
    import pyautogui

    pyautogui.hotkey(*SAFE_HOTKEYS[shortcut])
    return f"Pressed {shortcut}"


@action(
    "Scroll the current window up or down.",
    {
        "direction": {"type": "string", "enum": ["up", "down"]},
        "amount": {"type": "string", "enum": sorted(SCROLL_NOTCHES), "default": "medium"},
    },
    required=["direction"],
)
def scroll(direction: str, amount: str = "medium") -> str:
    """Scroll by a small, medium or large amount."""
    import pyautogui

    clicks = SCROLL_NOTCHES[amount] * WHEEL_DELTA
    pyautogui.scroll(clicks if direction == "up" else -clicks)
    return f"Scrolled {direction}"


@action("Take a screenshot and save it in the notes folder.")
def take_screenshot() -> str:
    """Save a PNG screenshot with a timestamped name."""
    import pyautogui

    folder = _config().notes_dir
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"screenshot_{datetime.now():%Y%m%d_%H%M%S}.png"
    pyautogui.screenshot().save(path)
    return f"Screenshot saved to {path}"


@action("Get battery level, charging state, CPU, memory and disk use.", informational=True)
def system_status() -> dict[str, Any]:
    """Report basic system health."""
    import psutil

    battery = psutil.sensors_battery()
    disk = psutil.disk_usage(Path.home().anchor or "/")
    return {
        "battery_percent": round(battery.percent) if battery else None,
        "charging": bool(battery.power_plugged) if battery else None,
        "cpu_percent": round(psutil.cpu_percent(interval=0.3)),
        "memory_percent": round(psutil.virtual_memory().percent),
        "disk_free_gb": round(disk.free / 1e9, 1),
        "disk_percent": round(disk.percent),
    }


@action("Lock the computer screen. Asks the user to confirm first.", risky=True)
def lock_pc() -> str:
    """Lock the Windows session."""
    _lock_workstation()
    return "Computer locked"


@action(
    "Shut down the computer after a delay. Asks the user to confirm first.", risky=True
)
def shutdown_pc() -> str:
    """Schedule a shutdown that the user can still cancel."""
    delay = _config().shutdown_delay_s
    subprocess.run(
        [SHUTDOWN_EXE, "/s", "/t", str(delay)], check=True, capture_output=True, timeout=10
    )
    return f"Shutdown in {delay} seconds. Say 'cancel shutdown' to stop it."


@action("Cancel a shutdown that was scheduled earlier.")
def cancel_shutdown() -> str:
    """Abort a pending shutdown."""
    done = subprocess.run([SHUTDOWN_EXE, "/a"], check=False, capture_output=True, timeout=10)
    if done.returncode != 0:
        return "No shutdown was scheduled"
    return "Shutdown cancelled"


@action(
    "Save a note, letter or application as a text file and open it. Write the COMPLETE "
    "text in 'content' in the user's language.",
    {
        "title": {"type": "string", "minLength": 1, "maxLength": MAX_TITLE_CHARS},
        "content": {"type": "string", "minLength": 1, "maxLength": MAX_NOTE_CHARS},
    },
    required=["title", "content"],
)
def save_note(title: str, content: str) -> str:
    """Write a UTF-8 text file in the notes folder, open it and return its path."""
    folder = _config().notes_dir.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    name = sanitize_filename(title)
    path = folder / f"{name}.txt"
    if path.exists():
        path = folder / f"{name} {datetime.now():%Y%m%d_%H%M%S}.txt"
    if path.resolve().parent != folder:
        raise ValueError("note path escapes the notes folder")
    path.write_text(content, encoding="utf-8")
    _startfile(str(path))
    return f"Saved note to {path}"


@action("Get the current local date and time.", informational=True)
def current_time() -> dict[str, str]:
    """Report the local date and time."""
    now = datetime.now()
    return {"time": now.strftime("%H:%M"), "date": now.strftime("%A, %d %B %Y")}

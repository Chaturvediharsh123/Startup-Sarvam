"""Volume, system status, screenshot, lock and delayed shutdown (PDF "Actions": system.py).

Shutdown always waits at least :data:`MIN_SHUTDOWN_DELAY_S` seconds so the user
can say "cancel shutdown". Only the fixed ``shutdown.exe`` under the Windows folder
is run, with fixed arguments and never through a shell.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from actions import registry
from actions.registry import ActionError, action

MIN_SHUTDOWN_DELAY_S = 60
# Full path so a planted shutdown.exe elsewhere on PATH can never be run.
SHUTDOWN_EXE = str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "shutdown.exe")


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


@action(
    "Set the speaker volume to an exact percentage.",
    {"level": {"type": "integer", "minimum": 0, "maximum": 100, "description": "0 to 100"}},
    required=["level"],
    risk="low",
)
def set_volume(level: int) -> str:
    """Set the master volume (0-100)."""
    if isinstance(level, bool) or not isinstance(level, int | float):
        raise ActionError("volume must be a number from 0 to 100")
    value = max(0, min(100, int(level)))
    endpoint = _endpoint_volume()
    endpoint.SetMasterVolumeLevelScalar(value / 100, None)
    if value > 0:
        endpoint.SetMute(0, None)
    return f"volume {value}"


@action(
    "Turn the volume up or down, or mute/unmute it.",
    {
        "direction": {"type": "string", "enum": ["up", "down", "mute", "unmute"]},
        "step": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
    },
    required=["direction"],
    risk="low",
)
def change_volume(direction: str, step: int = 10) -> str:
    """Change the master volume relative to its current level."""
    if direction not in ("up", "down", "mute", "unmute"):
        raise ActionError("volume direction must be up, down, mute or unmute")
    endpoint = _endpoint_volume()
    if direction == "mute":
        endpoint.SetMute(1, None)
        return "volume muted"
    if direction == "unmute":
        endpoint.SetMute(0, None)
        return "volume unmuted"
    step = max(1, min(50, int(step)))
    current = round(endpoint.GetMasterVolumeLevelScalar() * 100)
    level = max(0, min(100, current + (step if direction == "up" else -step)))
    endpoint.SetMasterVolumeLevelScalar(level / 100, None)
    endpoint.SetMute(0, None)
    return f"volume {level}"


@action("Take a screenshot and save it in the notes folder.", risk="low")
def take_screenshot() -> str:
    """Save a PNG screenshot with a timestamped name."""
    import pyautogui

    folder = registry.config().notes_dir
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"screenshot_{datetime.now():%Y%m%d_%H%M%S}.png"
    pyautogui.screenshot().save(path)
    return f"screenshot saved to {path}"


def _status_fact(status: dict[str, Any]) -> str:
    parts = []
    if status.get("battery_percent") is not None:
        charging = "charging" if status.get("charging") else "not charging"
        parts.append(f"battery {status['battery_percent']}% {charging}")
    parts.append(f"cpu {status['cpu_percent']}%")
    parts.append(f"memory {status['memory_percent']}%")
    parts.append(f"disk free {status['disk_free_gb']} GB")
    return ", ".join(parts)


@action(
    "Get battery level, charging state, CPU, memory and disk use.",
    risk="low",
    informational=True,
    fact=_status_fact,
)
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


@action("Lock the computer screen. Asks the user to confirm first.", risk="high")
def lock_pc() -> str:
    """Lock the Windows session."""
    registry._lock_workstation()
    return "pc locked"


def shutdown_delay() -> int:
    """Seconds before shutdown: the setting, but never less than one minute."""
    return max(MIN_SHUTDOWN_DELAY_S, int(registry.config().shutdown_delay_s))


@action(
    "Shut down the computer after a one-minute delay that can be cancelled. "
    "Asks the user to confirm first.",
    risk="high",
)
def shutdown_pc() -> str:
    """Schedule a shutdown that the user can still cancel."""
    delay = shutdown_delay()
    subprocess.run(
        [SHUTDOWN_EXE, "/s", "/t", str(delay)], check=True, capture_output=True, timeout=10
    )
    return f"shutdown in {delay} seconds, say cancel shutdown to stop it"


@action("Cancel a shutdown that was scheduled earlier.", risk="low")
def cancel_shutdown() -> str:
    """Abort a pending shutdown."""
    done = subprocess.run([SHUTDOWN_EXE, "/a"], check=False, capture_output=True, timeout=10)
    if done.returncode != 0:
        return "no shutdown was scheduled"
    return "shutdown cancelled"


def _time_fact(now: dict[str, str]) -> str:
    return f"time {now['time']}, {now['date']}"


@action("Get the current local date and time.", risk="low", informational=True, fact=_time_fact)
def current_time() -> dict[str, str]:
    """Report the local date and time."""
    now = datetime.now()
    return {"time": now.strftime("%H:%M"), "date": now.strftime("%A, %d %B %Y")}

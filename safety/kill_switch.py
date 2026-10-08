"""Emergency stop (PDF "Safety"): moving the mouse into a screen corner stops automation.

Two layers:

* pyautogui ``FAILSAFE``: any pyautogui call made while the mouse is in a corner
  raises ``FailSafeException`` (the action runner reports it as "cancelled").
* A watcher thread polls the mouse position and calls ``on_trigger`` once each
  time the mouse enters a corner (wire it to ``SafetyGuard.halt`` and the turn's
  cancel token).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from core.interfaces import Health

logger = logging.getLogger(__name__)

Position = Callable[[], tuple[int, int]]
ScreenSize = Callable[[], tuple[int, int]]


def enable_failsafe() -> bool:
    """Turn on the pyautogui failsafe. False when pyautogui is unavailable."""
    try:
        import pyautogui
    except Exception:  # noqa: BLE001 - no display / not installed
        logger.warning("pyautogui unavailable; the corner failsafe is off")
        return False
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05
    return True


def _mouse_position() -> tuple[int, int]:
    import pyautogui

    pos = pyautogui.position()
    return int(pos[0]), int(pos[1])


def _screen_size() -> tuple[int, int]:
    import pyautogui

    size = pyautogui.size()
    return int(size[0]), int(size[1])


class KillSwitch:
    """Calls ``on_trigger`` when the mouse sits in any screen corner."""

    def __init__(
        self,
        on_trigger: Callable[[], None],
        corner_px: int = 5,
        poll_s: float = 0.1,
        position: Position | None = None,
        screen_size: ScreenSize | None = None,
        failsafe: bool = True,
    ) -> None:
        self.on_trigger = on_trigger
        self.corner_px = corner_px
        self.poll_s = poll_s
        self._position = position or _mouse_position
        self._screen_size = screen_size or _screen_size
        self._failsafe = failsafe
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._in_corner = False
        self._error = ""
        self.triggers = 0

    def start(self) -> None:
        """Enable the pyautogui failsafe and start the watcher thread."""
        if self._failsafe and not enable_failsafe():
            self._error = "pyautogui unavailable"
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="kill-switch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the watcher thread."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=max(1.0, self.poll_s * 5))

    def health(self) -> Health:
        """OK while the watcher runs and can read the mouse."""
        running = self._thread is not None and self._thread.is_alive()
        if self._error:
            return Health("kill_switch", False, self._error)
        return Health("kill_switch", running, "watching corners" if running else "stopped")

    def in_corner(self, x: int, y: int, width: int, height: int) -> bool:
        """True when ``(x, y)`` is within ``corner_px`` of a screen corner."""
        c = self.corner_px
        near_x = x <= c or x >= width - 1 - c
        near_y = y <= c or y >= height - 1 - c
        return near_x and near_y

    def check(self) -> bool:
        """Poll once. Returns True when this poll fired the trigger."""
        try:
            x, y = self._position()
            width, height = self._screen_size()
        except Exception as exc:  # noqa: BLE001 - e.g. no display; keep watching
            if not self._error:
                logger.warning("Kill switch cannot read the mouse: %s", exc)
            self._error = f"cannot read mouse: {exc}"
            return False
        self._error = ""
        if not self.in_corner(x, y, width, height):
            self._in_corner = False
            return False
        if self._in_corner:
            return False  # already fired for this visit to the corner
        self._in_corner = True
        self.triggers += 1
        logger.warning("Emergency stop: mouse in a screen corner at (%d, %d)", x, y)
        try:
            self.on_trigger()
        except Exception:  # noqa: BLE001 - the watcher must keep running
            logger.exception("Kill switch callback failed")
        return True

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_s):
            self.check()

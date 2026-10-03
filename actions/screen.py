"""Visual fallback adapter (PyAutoGUI) and screen capture (mss).

Coordinate interaction is fragile: it is used only for ``screen`` targets or as an explicit
fallback, and captures before/after screenshots when ``visual_evidence`` is on. A screen
target may give visible ``text`` instead of a point; it is located with OCR (actions.ocr) on a
fresh screenshot right before acting. Moving the mouse into a screen corner triggers
PyAutoGUI's fail-safe and stops the action.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, NamedTuple

from actions.base import Adapter, AdapterOutcome, ExecutionContext, ThreadRunner
from actions.config import ActionSettings
from actions.errors import (
    ActionPermissionError,
    ActionValidationError,
    AdapterError,
    TargetNotFoundError,
    UnsupportedActionError,
)
from actions.models import (
    Action,
    ActionType,
    ClickParams,
    Condition,
    ConditionCheck,
    Evidence,
    HotkeyParams,
    PressKeyParams,
    ScrollParams,
    Target,
    TypeParams,
)
from actions.ocr import OcrEngine, TextMatch, create_ocr_engine, find_text

_PYAUTOGUI_KEYS = {"plus": "+", "minus": "-", "escape": "esc"}
_WHEEL_DELTA = 120  # one wheel notch on Windows


class XY(NamedTuple):
    x: int
    y: int


def grab_screen(path: Path) -> tuple[Path, XY]:
    """Capture all monitors into a PNG; also return the virtual-screen origin (negative when a
    monitor sits left of/above the primary one) to map image pixels to screen points."""
    import mss
    import mss.tools

    path.parent.mkdir(parents=True, exist_ok=True)
    with mss.mss() as sct:
        monitor = sct.monitors[0]
        shot = sct.grab(monitor)
        mss.tools.to_png(shot.rgb, shot.size, output=str(path))
    return path, XY(monitor["left"], monitor["top"])


def capture_screen(path: Path) -> Path:
    return grab_screen(path)[0]


def _send_unicode_text(text: str) -> None:
    """Type arbitrary Unicode (e.g. Devanagari) with SendInput, which pyautogui.write cannot."""
    import ctypes
    from ctypes import wintypes

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    INPUT_KEYBOARD, KEYEVENTF_UNICODE, KEYEVENTF_KEYUP = 1, 0x0004, 0x0002
    units = text.encode("utf-16-le")
    inputs: list[INPUT] = []
    for i in range(0, len(units), 2):
        code = int.from_bytes(units[i:i + 2], "little")
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            inputs.append(INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(wVk=0, wScan=code, dwFlags=flags, time=0, dwExtraInfo=0)))
    array = (INPUT * len(inputs))(*inputs)
    sent = ctypes.windll.user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise AdapterError(f"SendInput delivered {sent}/{len(inputs)} key events (input may be blocked)")


class VisualAdapter(Adapter):
    name = "visual"
    supports = frozenset({
        ActionType.CLICK, ActionType.TYPE, ActionType.PRESS_KEY, ActionType.HOTKEY,
        ActionType.SCROLL, ActionType.SCREENSHOT,
    })

    _UNSET: Any = object()

    def __init__(self, settings: ActionSettings, runner: ThreadRunner | None = None, ocr: Any = _UNSET) -> None:
        self.settings = settings
        self._runner = runner or ThreadRunner("visual")
        self._ocr: Any = ocr  # OcrEngine | None; built lazily from settings unless injected

    def available(self) -> bool:
        return all(importlib.util.find_spec(m) is not None for m in ("pyautogui", "mss"))

    async def execute(self, action: Action, ctx: ExecutionContext) -> AdapterOutcome:
        return await self._runner.run(self._execute_sync, action, ctx)

    async def screenshot(self, path: Path, target: Target | None = None) -> Path | None:
        return await self._runner.run(capture_screen, path)

    async def check(self, condition: Condition, ctx: ExecutionContext) -> ConditionCheck:
        return await self._runner.run(self._check_sync, condition, ctx)

    async def close(self) -> None:
        self._runner.shutdown()

    async def warm_up(self) -> None:
        def load() -> None:
            import mss  # noqa: F401

            self._pyautogui()

        await self._runner.run(load)

    # ------------------------------------------------------------------ sync side

    @staticmethod
    def _pyautogui() -> Any:
        import pyautogui

        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.05
        return pyautogui

    def _execute_sync(self, action: Action, ctx: ExecutionContext) -> AdapterOutcome:
        pg = self._pyautogui()
        try:
            return self._dispatch(pg, action, ctx)
        except pg.FailSafeException:
            raise ActionPermissionError("PyAutoGUI fail-safe triggered (mouse in a screen corner); automation stopped",
                                        adapter=self.name, may_have_side_effects=True) from None

    def _dispatch(self, pg: Any, action: Action, ctx: ExecutionContext) -> AdapterOutcome:
        target = action.target
        out = AdapterOutcome()
        p = action.parameters
        point = XY(target.point.x, target.point.y) if target is not None and target.point is not None else None
        if target is not None and target.text is not None and action.type is not ActionType.SCREENSHOT:
            match, origin, shot = self._locate(target.text, ctx, action.action_id)
            point = XY(match.center[0] + origin.x, match.center[1] + origin.y)
            out.evidence.append(Evidence(type="ui_element", value=match.text, metadata={
                "located_by": f"ocr:{self._engine().name}", "point": point._asdict(), "screenshot": str(shot),
                "box": {"left": match.left + origin.x, "top": match.top + origin.y,
                        "width": match.width, "height": match.height}}))

        if action.type is ActionType.SCREENSHOT:
            path = capture_screen(ctx.evidence_path(action.action_id, "screen"))
            out.output = {"path": str(path)}
            out.evidence.append(Evidence(type="screenshot", value=str(path)))
            return out

        if action.type is ActionType.CLICK and point is None:
            raise ActionValidationError("visual click needs target.point or target.text", adapter=self.name)

        self._capture(out, action, ctx, "before", point)
        if action.type is ActionType.CLICK:
            assert isinstance(p, ClickParams) and point is not None
            pg.click(point.x, point.y, clicks=2 if p.double else 1, button=p.button)
            out.output = {"clicked": {"x": point.x, "y": point.y}}
        elif action.type is ActionType.TYPE:
            assert isinstance(p, TypeParams)
            if point is not None:
                pg.click(point.x, point.y)
            if p.clear_first:
                pg.hotkey("ctrl", "a")
                pg.press("backspace")
            self._type_text(pg, p.text)
            if p.submit:
                pg.press("enter")
            out.output = {"typed_chars": len(p.text)}
        elif action.type is ActionType.PRESS_KEY:
            assert isinstance(p, PressKeyParams)
            pg.press(_PYAUTOGUI_KEYS.get(p.key, p.key), presses=p.presses, interval=0.05)
            out.output = {"key": p.key, "presses": p.presses}
        elif action.type is ActionType.HOTKEY:
            assert isinstance(p, HotkeyParams)
            pg.hotkey(*[_PYAUTOGUI_KEYS.get(k, k) for k in p.keys])
            out.output = {"hotkey": p.combo}
        elif action.type is ActionType.SCROLL:
            assert isinstance(p, ScrollParams)
            self._scroll(pg, p, point)
            out.output = {"direction": p.direction, "amount": p.amount}
        self._capture(out, action, ctx, "after", point)
        return out

    @staticmethod
    def _type_text(pg: Any, text: str) -> None:
        if text.isascii():
            pg.write(text, interval=0.01)
            return
        if sys.platform != "win32":
            raise AdapterError("non-ASCII typing via the visual adapter is only implemented on Windows")
        for i, line in enumerate(text.split("\n")):
            if i:
                pg.press("enter")
            if line:
                _send_unicode_text(line)

    @staticmethod
    def _scroll(pg: Any, p: ScrollParams, point: Any) -> None:
        x, y = (point.x, point.y) if point is not None else (None, None)
        sign = 1 if p.direction in ("up", "left") else -1
        clicks = sign * p.amount * (_WHEEL_DELTA if sys.platform == "win32" else 1)
        if p.direction in ("up", "down"):
            pg.scroll(clicks, x=x, y=y)
        else:
            # PyAutoGUI has no real horizontal scroll on Windows; shift+wheel is the standard equivalent.
            pg.keyDown("shift")
            try:
                pg.scroll(clicks, x=x, y=y)
            finally:
                pg.keyUp("shift")

    def _capture(self, out: AdapterOutcome, action: Action, ctx: ExecutionContext, label: str, point: Any) -> None:
        if not self.settings.visual_evidence:
            return
        try:
            path = capture_screen(ctx.evidence_path(action.action_id, label))
        except Exception as exc:  # evidence must never break the interaction itself
            out.evidence.append(Evidence(type="screenshot", value="", metadata={"phase": label, "error": str(exc)}))
            return
        meta: dict[str, Any] = {"phase": label}
        if point is not None:
            meta["point"] = {"x": point.x, "y": point.y}
        out.evidence.append(Evidence(type="screenshot", value=str(path), metadata=meta))

    # ------------------------------------------------------------------ OCR

    def _engine(self) -> OcrEngine:
        if self._ocr is VisualAdapter._UNSET:
            self._ocr = create_ocr_engine(self.settings)
        if self._ocr is None:
            raise UnsupportedActionError(
                "no OCR engine available: install the winrt Windows.Media.Ocr packages or Tesseract",
                adapter=self.name, details={"ocr_engine": self.settings.ocr_engine})
        return self._ocr

    def _find(self, text: str, path: Path) -> tuple[list[TextMatch], XY]:
        engine = self._engine()
        path, origin = grab_screen(path)
        return find_text(engine.recognize(path), text), origin

    def _locate(self, text: str, ctx: ExecutionContext, action_id: str) -> tuple[TextMatch, XY, Path]:
        shot = ctx.evidence_path(action_id, "ocr")
        matches, origin = self._find(text, shot)
        if not matches:
            raise TargetNotFoundError(f"text {text!r} not found on screen (OCR: {self._engine().name})",
                                      adapter=self.name, target={"surface": "screen", "text": text},
                                      evidence=[Evidence(type="screenshot", value=str(shot), metadata={"phase": "ocr"})])
        return matches[0], origin, shot

    def _check_sync(self, condition: Condition, ctx: ExecutionContext) -> ConditionCheck:
        target = condition.target
        text = condition.value if condition.kind == "text_present" else (target.text if target else None)
        if condition.kind not in ("text_present", "element_visible", "element_hidden") or not text:
            return ConditionCheck(condition=condition, status="unverified",
                                  detail=f"visual adapter cannot check {condition.kind}")
        matches, _ = self._find(text, ctx.evidence_path("check", "ocr"))
        found = bool(matches)
        ok = not found if condition.kind == "element_hidden" else found
        return ConditionCheck(condition=condition, status="satisfied" if ok else "unsatisfied",
                              observed=matches[0].text if matches else None, detail=f"OCR: {self._engine().name}")

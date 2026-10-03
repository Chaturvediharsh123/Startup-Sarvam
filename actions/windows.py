"""Windows application adapter (pywinauto, UI Automation backend).

Interacts with real controls located by window title, control name, control type and
automation id. Apps are started from the configured allow-list without a shell and are
closed gracefully (the app may still show its own "save changes?" prompt; nothing is killed).
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from actions.base import Adapter, AdapterOutcome, ExecutionContext, ThreadRunner
from actions.config import ActionSettings
from actions.errors import ActionValidationError, AdapterError, TargetNotFoundError
from actions.models import (
    MODIFIERS,
    Action,
    ActionType,
    ClickParams,
    CloseAppParams,
    Condition,
    ConditionCheck,
    Evidence,
    HotkeyParams,
    OpenAppParams,
    PressKeyParams,
    ReadParams,
    ScrollParams,
    Target,
    TypeParams,
)

_SEND_KEYS_SPECIAL = set("+^%~(){}")
_NAMED_KEYS = {
    "enter": "ENTER", "tab": "TAB", "escape": "ESC", "backspace": "BACKSPACE", "delete": "DELETE",
    "space": "SPACE", "up": "UP", "down": "DOWN", "left": "LEFT", "right": "RIGHT", "home": "HOME",
    "end": "END", "pageup": "PGUP", "pagedown": "PGDN", **{f"f{i}": f"F{i}" for i in range(1, 13)},
}
_MODIFIER_PREFIX = {"ctrl": "^", "shift": "+", "alt": "%"}
_NEW_WINDOW_GRACE_S = 5.0


def escape_send_keys(text: str) -> str:
    """Make text literal for pywinauto's send_keys/type_keys syntax."""
    return "".join("{" + c + "}" if c in _SEND_KEYS_SPECIAL else c for c in text)


def key_sequence(key: str, presses: int = 1) -> str:
    if key in _NAMED_KEYS:
        return "{" + _NAMED_KEYS[key] + (f" {presses}" if presses > 1 else "") + "}"
    token = {"plus": "{+}", "minus": "-"}.get(key, escape_send_keys(key))
    return token * presses


def hotkey_sequence(keys: list[str]) -> str:
    mods = [k for k in keys if k in MODIFIERS]
    key = next(k for k in keys if k not in MODIFIERS)
    seq = "".join(_MODIFIER_PREFIX[m] for m in mods if m != "win") + key_sequence(key)
    if "win" in mods:
        seq = "{VK_LWIN down}" + seq + "{VK_LWIN up}"
    return seq


def _init_com() -> None:
    try:
        import pythoncom

        pythoncom.CoInitializeEx(getattr(sys, "coinit_flags", 0))
    except Exception:
        pass


class WindowsAdapter(Adapter):
    name = "windows"
    supports = frozenset({
        ActionType.OPEN_APP, ActionType.CLOSE_APP, ActionType.CLICK, ActionType.TYPE, ActionType.PRESS_KEY,
        ActionType.HOTKEY, ActionType.SCROLL, ActionType.READ, ActionType.SCREENSHOT,
    })

    def __init__(self, settings: ActionSettings, runner: ThreadRunner | None = None) -> None:
        self.settings = settings
        self._runner = runner or ThreadRunner("uia", initializer=_init_com)

    def available(self) -> bool:
        return sys.platform == "win32" and importlib.util.find_spec("pywinauto") is not None

    async def execute(self, action: Action, ctx: ExecutionContext) -> AdapterOutcome:
        return await self._runner.run(self._dispatch, action, ctx)

    async def check(self, condition: Condition, ctx: ExecutionContext) -> ConditionCheck:
        return await self._runner.run(self._check_sync, condition)

    async def screenshot(self, path: Path, target: Target | None = None) -> Path | None:
        if target is None or target.surface != "window":
            return None
        return await self._runner.run(self._screenshot_sync, path, target)

    async def close(self) -> None:
        self._runner.shutdown()

    async def warm_up(self) -> None:
        # First use of UI Automation generates COM wrappers and enumerates the desktop; this
        # took ~10 s on a cold machine, so do it before the user's first command.
        await self._runner.run(lambda: self._desktop().windows())

    # ------------------------------------------------------------------ locating

    @staticmethod
    def _desktop() -> Any:
        from pywinauto import Desktop

        return Desktop(backend="uia")

    def _window_spec(self, title_re: str) -> Any:
        try:
            re.compile(title_re)
        except re.error as exc:
            raise ActionValidationError(f"invalid window title regex {title_re!r}: {exc}") from None
        return self._desktop().window(title_re=title_re, found_index=0)

    def _find_window(self, title_re: str, timeout: float) -> Any:
        spec = self._window_spec(title_re)
        try:
            spec.wait("exists visible", timeout=timeout, retry_interval=0.2)
        except Exception:
            raise TargetNotFoundError(f"no visible window matching {title_re!r}", target={"window": title_re}) from None
        return spec

    def _control_spec(self, window_spec: Any, target: Target) -> Any:
        criteria: dict[str, Any] = {"found_index": 0}
        if target.name:
            criteria["title"] = target.name
        if target.control_type:
            criteria["control_type"] = target.control_type
        if target.automation_id:
            criteria["auto_id"] = target.automation_id
        return window_spec.child_window(**criteria)

    def _resolve(self, target: Target, ctx: ExecutionContext) -> tuple[Any, Any]:
        """Return (window wrapper, element wrapper). The element is the window itself for
        window-level targets."""
        assert target.window is not None
        window_spec = self._find_window(target.window, ctx.find_timeout())
        window = window_spec.wrapper_object()
        if not target.is_element:
            return window, window
        spec = self._control_spec(window_spec, target)
        try:
            spec.wait("exists visible", timeout=ctx.find_timeout(), retry_interval=0.2)
        except Exception:
            raise TargetNotFoundError("control not found or not visible", target=target.summary()) from None
        return window, spec.wrapper_object()

    def _handles(self, title_re: str) -> set[int]:
        return {w.handle for w in self._desktop().windows(title_re=title_re)}

    # ------------------------------------------------------------------ actions

    def _dispatch(self, action: Action, ctx: ExecutionContext) -> AdapterOutcome:
        t, p = action.type, action.parameters
        if t is ActionType.OPEN_APP:
            return self._open_app(p, ctx)
        if t is ActionType.CLOSE_APP:
            return self._close_app(p)

        from pywinauto.keyboard import send_keys

        assert action.target is not None  # registry/router guarantee a window target here
        window, el = self._resolve(action.target, ctx)
        out = AdapterOutcome()

        if t is ActionType.CLICK:
            assert isinstance(p, ClickParams)
            el.click_input(button=p.button, double=p.double)
            out.output = {"clicked": el.window_text()}
            out.evidence.append(self._element_evidence(el))
        elif t is ActionType.TYPE:
            assert isinstance(p, TypeParams)
            self._type(el, p)
            out.output = {"typed_chars": len(p.text)}
        elif t in (ActionType.PRESS_KEY, ActionType.HOTKEY):
            el.set_focus()
            if isinstance(p, PressKeyParams):
                send_keys(key_sequence(p.key, p.presses))
                out.output = {"key": p.key, "presses": p.presses}
            else:
                assert isinstance(p, HotkeyParams)
                send_keys(hotkey_sequence(p.keys))
                out.output = {"hotkey": p.combo}
        elif t is ActionType.SCROLL:
            assert isinstance(p, ScrollParams)
            self._scroll(el, p)
            out.output = {"direction": p.direction, "amount": p.amount}
        elif t is ActionType.READ:
            assert isinstance(p, ReadParams)
            text = self._read_text(el, element=action.target.is_element)
            out.output = {"text": text[: p.max_chars], "truncated": len(text) > p.max_chars}
            out.evidence.append(Evidence(type="observed_text", value=text[:500]))
        elif t is ActionType.SCREENSHOT:
            path = ctx.evidence_path(action.action_id, "window")
            el.capture_as_image().save(path)
            out.output = {"path": str(path)}
            out.evidence.append(Evidence(type="screenshot", value=str(path)))
        else:  # pragma: no cover - guarded by `supports`
            raise AdapterError(f"windows adapter cannot run {t.value}")

        out.evidence.append(Evidence(type="window_title", value=window.window_text()))
        return out

    def _open_app(self, p: OpenAppParams, ctx: ExecutionContext) -> AdapterOutcome:
        entry = self.settings.apps[p.app]
        before = self._handles(entry.window_title_re)
        proc = subprocess.Popen(entry.command, shell=False, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Prefer a window that did not exist before; single-instance apps may reuse an old one.
        deadline = time.monotonic() + min(ctx.remaining(), _NEW_WINDOW_GRACE_S)
        window = None
        while time.monotonic() < deadline:
            fresh = [w for w in self._desktop().windows(title_re=entry.window_title_re) if w.handle not in before]
            if fresh:
                window = fresh[0]
                break
            time.sleep(0.1)
        new_window = window is not None
        if window is None:
            try:
                window = self._find_window(entry.window_title_re, max(0.5, ctx.remaining() - 0.5)).wrapper_object()
            except TargetNotFoundError:
                raise AdapterError(f"started '{p.app}' but no window matching {entry.window_title_re!r} appeared",
                                   may_have_side_effects=True, details={"pid": proc.pid}) from None
        try:
            window.set_focus()
        except Exception:
            pass
        title = window.window_text()
        return AdapterOutcome(
            output={"app": p.app, "pid": proc.pid, "window_title": title, "new_window": new_window},
            evidence=[Evidence(type="window_title", value=title, metadata={"handle": window.handle, "new_window": new_window})],
        )

    def _close_app(self, p: CloseAppParams) -> AdapterOutcome:
        entry = self.settings.apps[p.app]
        spec = self._window_spec(entry.window_title_re)
        if not spec.exists(timeout=2):
            raise TargetNotFoundError(f"no open window for '{p.app}'", target={"window": entry.window_title_re})
        window = spec.wrapper_object()
        title, handle = window.window_text(), window.handle
        window.close()
        closed = False
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if handle not in self._handles(entry.window_title_re):
                closed = True
                break
            time.sleep(0.2)
        return AdapterOutcome(
            output={"app": p.app, "window_title": title, "closed": closed},
            evidence=[Evidence(type="window_title", value=title,
                               metadata={"closed": closed, "note": None if closed else "window still open (the app may be asking to save)"})],
        )

    @staticmethod
    def _type(el: Any, p: TypeParams) -> None:
        if p.clear_first:
            try:
                el.set_edit_text(p.text)  # ValuePattern: fast and Unicode-safe
            except Exception:
                el.type_keys("^a{BACKSPACE}")
                el.type_keys(escape_send_keys(p.text), with_spaces=True, with_tabs=True, with_newlines=True, pause=0.01)
        else:
            el.type_keys(escape_send_keys(p.text), with_spaces=True, with_tabs=True, with_newlines=True, pause=0.01)
        if p.submit:
            el.type_keys("{ENTER}")

    @staticmethod
    def _scroll(el: Any, p: ScrollParams) -> None:
        if p.direction in ("up", "down"):
            from pywinauto import mouse

            mid = el.rectangle().mid_point()
            mouse.scroll(coords=(mid.x, mid.y), wheel_dist=p.amount if p.direction == "up" else -p.amount)
            return
        try:
            el.scroll(p.direction, "line", p.amount)
        except Exception as exc:
            raise AdapterError(f"horizontal scroll not supported by this control: {exc}", may_have_side_effects=False) from None

    @staticmethod
    def _read_text(el: Any, *, element: bool) -> str:
        if element:
            for getter in (lambda: el.get_value(), lambda: el.iface_value.CurrentValue, lambda: el.window_text()):
                try:
                    value = getter()
                except Exception:
                    continue
                if value:
                    return str(value)
        texts: list[str] = []
        for child in el.descendants()[:500]:
            try:
                text = child.window_text()
            except Exception:
                continue
            if text and (not texts or texts[-1] != text):
                texts.append(text)
        return "\n".join(texts)

    @staticmethod
    def _element_evidence(el: Any) -> Evidence:
        info = el.element_info
        return Evidence(type="ui_element", value=el.window_text(),
                        metadata={"control_type": info.control_type, "automation_id": info.automation_id})

    # ------------------------------------------------------------------ checks / evidence

    def _check_sync(self, condition: Condition) -> ConditionCheck:
        target = condition.target
        if target is None or target.surface != "window" or target.window is None:
            return ConditionCheck(condition=condition, status="unverified", detail="windows adapter needs a window target")
        window_spec = self._window_spec(target.window)
        window_exists = window_spec.exists(timeout=0)
        kind = condition.kind

        def result(ok: bool, observed: str | None = None) -> ConditionCheck:
            return ConditionCheck(condition=condition, status="satisfied" if ok else "unsatisfied", observed=observed)

        if kind == "window_exists":
            return result(window_exists)
        if kind == "window_absent":
            return result(not window_exists)
        if kind in ("element_visible", "element_hidden"):
            visible = False
            if window_exists:
                spec = self._control_spec(window_spec, target)
                visible = spec.exists(timeout=0) and spec.wrapper_object().is_visible()
            return result(visible if kind == "element_visible" else not visible)
        if kind == "text_present":
            if not window_exists:
                return result(False)
            el = window_spec.wrapper_object()
            if target.is_element:
                spec = self._control_spec(window_spec, target)
                if not spec.exists(timeout=0):
                    return result(False)
                el = spec.wrapper_object()
            text = self._read_text(el, element=target.is_element)
            return result((condition.value or "").casefold() in text.casefold(), observed=text[:200])
        return ConditionCheck(condition=condition, status="unverified", detail=f"windows adapter cannot check {kind}")

    def _screenshot_sync(self, path: Path, target: Target) -> Path | None:
        assert target.window is not None
        spec = self._window_spec(target.window)
        if not spec.exists(timeout=1):
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        spec.wrapper_object().capture_as_image().save(path)
        return path

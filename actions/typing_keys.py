"""Typing, safe shortcuts and scrolling (PDF "Actions": typing_keys.py).

Text is pasted through the clipboard so every script works (Hindi, Tamil, ...),
and the old clipboard is put back. Only shortcuts from the allow-list
``safe_shortcuts`` can be pressed; ``blocked_key_combos`` are refused even if a
safe-list entry were edited to contain one.
"""

from __future__ import annotations

from actions import registry
from actions.registry import ActionError, NotAllowedError, action

# Mouse-wheel notches; Windows reports one notch as a delta of 120.
SCROLL_NOTCHES: dict[str, int] = {"small": 3, "medium": 8, "large": 15}
WHEEL_DELTA = 120


@action(
    "Type text into the window that is open now. Works for every language and script.",
    {"text": {"type": "string", "minLength": 1, "maxLength": 2000}},
    required=["text"],
    risk="medium",
)
def type_text(text: str) -> str:
    """Paste ``text`` via the clipboard and restore the previous clipboard."""
    import pyautogui
    import pyperclip

    if not isinstance(text, str) or not text:
        raise ActionError("nothing to type")
    if len(text) > registry.limit("text_max_chars", 2000):
        raise ActionError("text is too long to type")
    try:
        previous = pyperclip.paste()
    except pyperclip.PyperclipException:
        previous = None
    pyperclip.copy(text)
    try:
        registry._sleep(0.05)
        pyautogui.hotkey("ctrl", "v")
        registry._sleep(0.3)
    finally:
        if previous is not None:
            pyperclip.copy(previous)
    return f"typed {len(text)} characters"


def _is_blocked(keys: tuple[str, ...]) -> bool:
    data = registry.allowlist()
    combo = frozenset(keys)
    if combo & {str(k).lower() for k in data["blocked_keys"]}:
        return True
    return any(combo == frozenset(str(k).lower() for k in c) for c in data["blocked_key_combos"])


@action(
    "Press a common keyboard shortcut in the current window.",
    {"shortcut": {"type": "string", "x-enum": "safe_shortcuts"}},
    required=["shortcut"],
    risk="medium",
)
def press_shortcut(shortcut: str) -> str:
    """Press one allow-listed key combination."""
    import pyautogui

    keys = registry.safe_shortcuts().get(str(shortcut).strip().lower())
    if keys is None or _is_blocked(keys):
        raise NotAllowedError(f"{str(shortcut)[:30]} is not a safe shortcut")
    pyautogui.hotkey(*keys)
    return f"pressed {shortcut.replace('_', ' ')}"


@action(
    "Scroll the current window up or down.",
    {
        "direction": {"type": "string", "enum": ["up", "down"]},
        "amount": {"type": "string", "enum": sorted(SCROLL_NOTCHES), "default": "medium"},
    },
    required=["direction"],
    risk="low",
)
def scroll(direction: str, amount: str = "medium") -> str:
    """Scroll by a small, medium or large amount."""
    import pyautogui

    if direction not in ("up", "down") or amount not in SCROLL_NOTCHES:
        raise ActionError("scroll must be up or down, small, medium or large")
    clicks = SCROLL_NOTCHES[amount] * WHEEL_DELTA
    pyautogui.scroll(clicks if direction == "up" else -clicks)
    return f"scrolled {direction}"

"""Colours and font sizes for the window (PDF WS7 "Accessibility").

Three palettes: ``dark``, ``light`` and ``high_contrast`` (black background,
white/yellow text, thick focus rings). Fonts use Nirmala UI so Devanagari,
Tamil, Telugu, Bengali, Kannada and Gujarati render correctly. The base size is
never below 18 pt; headings are 1.4 x the base size.

Pure data and functions only: nothing here needs a display.
"""

from __future__ import annotations

from dataclasses import dataclass

FONT_FAMILY = "Nirmala UI"
MIN_FONT_SIZE = 18
MAX_FONT_SIZE = 32
DEFAULT_FONT_SIZE = 20
BIG_FACTOR = 1.4
MIN_SMALL_FONT_SIZE = 16


@dataclass(frozen=True)
class Palette:
    """Every colour the window uses. Values are ``#rrggbb`` strings."""

    name: str
    appearance: str  # customtkinter appearance mode: "dark" or "light"
    bg: str  # window background
    panel: str  # section background
    text: str  # strong text, e.g. the final transcript
    muted: str  # secondary text, e.g. the grey partial transcript
    heading: str  # section headings
    accent: str  # normal buttons and switches
    accent_hover: str
    accent_text: str
    danger: str  # the Stop button
    danger_hover: str
    danger_text: str
    ok: str  # healthy module dot, successful action
    bad: str  # broken module dot, failed or denied action, errors
    unknown: str  # module not reported yet
    warn: str  # waiting for a yes / no
    focus: str  # keyboard focus ring
    focus_width: int
    border_width: int  # outline around text boxes (thick in high contrast)
    entry_bg: str
    track: str  # switch track / level meter background

    @property
    def partial(self) -> str:
        """Colour of not-yet-final transcript text ("grey")."""
        return self.muted

    @property
    def final(self) -> str:
        """Colour of final transcript text ("black" in light, white in dark)."""
        return self.text


PALETTES: dict[str, Palette] = {
    "dark": Palette(
        name="dark", appearance="dark", bg="#1e1f22", panel="#2b2d31", text="#ffffff",
        muted="#a0a0a0", heading="#e8e8e8", accent="#1f6aa5", accent_hover="#2a80c8",
        accent_text="#ffffff", danger="#b3261e", danger_hover="#d63a30", danger_text="#ffffff",
        ok="#3ddc84", bad="#ff6b6b", unknown="#8a8a8a", warn="#ffb74d", focus="#4fc3f7",
        focus_width=3, border_width=1, entry_bg="#18191b", track="#4a4d52",
    ),
    "light": Palette(
        name="light", appearance="light", bg="#f2f2f2", panel="#ffffff", text="#000000",
        muted="#666666", heading="#1a1a1a", accent="#0b57d0", accent_hover="#0842a0",
        accent_text="#ffffff", danger="#b3261e", danger_hover="#8c1d18", danger_text="#ffffff",
        ok="#1e8e3e", bad="#c5221f", unknown="#757575", warn="#a05a00", focus="#e8710a",
        focus_width=3, border_width=1, entry_bg="#ffffff", track="#c4c4c4",
    ),
    "high_contrast": Palette(
        name="high_contrast", appearance="dark", bg="#000000", panel="#000000", text="#ffffff",
        muted="#c8c8c8", heading="#ffff00", accent="#ffff00", accent_hover="#ffffff",
        accent_text="#000000", danger="#d00000", danger_hover="#ff2020", danger_text="#ffffff",
        ok="#00ff00", bad="#ff5050", unknown="#b0b0b0", warn="#ffff00", focus="#00ffff",
        focus_width=5, border_width=2, entry_bg="#000000", track="#606060",
    ),
}


def palette(name: str | None) -> Palette:
    """The palette called ``name``; unknown names fall back to ``dark``."""
    return PALETTES.get((name or "").strip().lower(), PALETTES["dark"])


@dataclass(frozen=True)
class FontSpec:
    """Font family and point sizes for one base size."""

    family: str
    base: int
    big: int
    small: int


def clamp_font_size(size: int | str | None) -> int:
    """A usable base size between :data:`MIN_FONT_SIZE` and :data:`MAX_FONT_SIZE`."""
    try:
        value = int(size) if size is not None else DEFAULT_FONT_SIZE
    except (TypeError, ValueError):
        value = DEFAULT_FONT_SIZE
    return max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, value))


def font_spec(size: int | str | None) -> FontSpec:
    """Font sizes for a base size: big = base x 1.4, small = base - 2 (at least 16)."""
    base = clamp_font_size(size)
    return FontSpec(
        family=FONT_FAMILY,
        base=base,
        big=round(base * BIG_FACTOR),
        small=max(MIN_SMALL_FONT_SIZE, base - 2),
    )


def health_colour(pal: Palette, ok: bool | None) -> str:
    """Dot colour for a module: green ok, red broken, grey not reported yet."""
    if ok is None:
        return pal.unknown
    return pal.ok if ok else pal.bad


def status_colour(pal: Palette, status: str) -> str:
    """Text colour for an action status (``ok`` green, failures red, others muted)."""
    if status == "ok":
        return pal.ok
    if status in ("error", "denied", "blocked", "timeout", "deny"):
        return pal.bad
    if status in ("confirm", "cancelled"):
        return pal.warn
    return pal.text

"""Labels (English + Hindi) and theme palettes: pure data, no display needed."""

from __future__ import annotations

import re

import pytest

from core.config import UI_THEMES
from core.events import TurnState
from ui.labels import LABELS, LANGUAGE_HINTS, hint_choice, hint_choices, hint_code, t, ui_lang
from ui.theme import (
    BIG_FACTOR,
    MIN_FONT_SIZE,
    PALETTES,
    clamp_font_size,
    font_spec,
    health_colour,
    palette,
    status_colour,
)
from ui.widgets import HEALTH_MODULES, STATUS_MARKS

DEVANAGARI = re.compile("[ऀ-ॿ]")
HEX = re.compile(r"^#[0-9a-f]{6}$")


def test_every_label_has_english_and_hindi() -> None:
    for key, entry in LABELS.items():
        assert entry.get("en"), key
        assert entry.get("hi"), key
        assert DEVANAGARI.search(entry["hi"]), f"{key}: Hindi label is not in Devanagari"


def test_placeholders_match_between_languages() -> None:
    for key, entry in LABELS.items():
        fields_en = set(re.findall(r"{(\w+)}", entry["en"]))
        fields_hi = set(re.findall(r"{(\w+)}", entry["hi"]))
        assert fields_en == fields_hi, key


def test_states_modules_and_statuses_have_labels() -> None:
    for state in TurnState:
        assert f"state_{state.value}" in LABELS
    for module in HEALTH_MODULES:
        assert f"mod_{module}" in LABELS
    for status in STATUS_MARKS:
        assert f"status_{status}" in LABELS
    for verdict in ("allow", "confirm", "deny"):
        assert f"verdict_{verdict}" in LABELS


def test_t_switches_language_and_formats() -> None:
    assert t("stop", "en") == "Stop (F9)"
    assert t("stop", "hi") == "रोकें (F9)"
    assert t("stop", "hi-IN") == "रोकें (F9)"
    assert t("stop", None) == "Stop (F9)"
    assert t("problem", "en", message="mic busy") == "Problem: mic busy"
    assert t("no_such_label", "hi") == "no_such_label"
    assert ui_lang("HI") == "hi" and ui_lang("ta") == "en"


def test_language_hint_choices() -> None:
    choices = hint_choices("en")
    assert len(choices) == len(LANGUAGE_HINTS) == 10
    assert len(set(choices)) == len(choices)
    assert choices[0] == "Auto"
    assert hint_choices("hi")[0] == "अपने आप"
    assert hint_code("Auto", "en") is None
    assert hint_code("Hinglish · hi-IN") == "hi-IN"  # romanised hint is still hi-IN
    assert hint_code("தமிழ் · ta-IN") == "ta-IN"
    assert hint_choice("hi-IN") == "हिन्दी · hi-IN"
    assert hint_choice(None, "hi") == "अपने आप"
    assert hint_choice("xx-YY") == "Auto"
    codes = {code for _, code in LANGUAGE_HINTS}
    assert codes == {"", "hi-IN", "en-IN", "ta-IN", "te-IN", "bn-IN", "kn-IN", "mr-IN", "gu-IN"}


def test_palettes_cover_every_theme_with_valid_colours() -> None:
    assert set(PALETTES) == set(UI_THEMES)
    for pal in PALETTES.values():
        for name, value in vars(pal).items():
            if isinstance(value, str) and value.startswith("#"):
                assert HEX.match(value), (pal.name, name, value)
        assert pal.partial != pal.final
        assert pal.appearance in ("dark", "light")


def test_high_contrast_and_light_palettes() -> None:
    hc = palette("high_contrast")
    assert hc.bg == "#000000"
    assert hc.final == "#ffffff"
    assert hc.focus_width > palette("dark").focus_width
    assert palette("light").final == "#000000"  # "final text in black"
    assert palette("dark").final == "#ffffff"
    assert palette("nonsense").name == "dark"
    assert palette(None).name == "dark"


def test_font_sizes_never_below_18() -> None:
    assert font_spec(10).base == MIN_FONT_SIZE
    spec = font_spec(20)
    assert spec.big == round(20 * BIG_FACTOR) == 28
    assert spec.small >= 16
    assert spec.family == "Nirmala UI"
    assert clamp_font_size("24") == 24
    assert clamp_font_size("big") == 20
    assert clamp_font_size(99) == 32


@pytest.mark.parametrize(("ok", "attr"), [(True, "ok"), (False, "bad"), (None, "unknown")])
def test_health_colour(ok: bool | None, attr: str) -> None:
    pal = palette("dark")
    assert health_colour(pal, ok) == getattr(pal, attr)


def test_status_colour() -> None:
    pal = palette("light")
    assert status_colour(pal, "ok") == pal.ok
    assert status_colour(pal, "denied") == pal.bad
    assert status_colour(pal, "confirm") == pal.warn

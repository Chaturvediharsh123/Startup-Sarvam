"""``language`` re-exports exactly the public API of ``language.detect``."""

from __future__ import annotations

import pytest

import language
import language.detect as detect


def test_all_names_come_from_detect() -> None:
    for name in language.__all__:
        assert getattr(language, name) is getattr(detect, name)


def test_public_api_covers_what_other_modules_import() -> None:
    expected = {
        "LanguageInfo", "detect_language", "detect_script", "normalise_code",
        "to_tts_language", "to_realtime_stt_code",
    }
    assert expected <= set(language.__all__)


def test_package_level_detection() -> None:
    info = language.detect_language("Chrome kholo")
    assert info == language.LanguageInfo(code="hi-IN", script="latin", romanised=True)


@pytest.mark.parametrize("text", ["sab bhool jao", "ruko", "haan ji", "ise band karo"])
def test_short_hinglish_commands_are_hindi(text: str) -> None:
    from language.detect import detect_language

    info = detect_language(text)
    assert info.code == "hi-IN" and info.romanised


@pytest.mark.parametrize("text", ["use google", "what is the time", "open chrome"])
def test_plain_english_stays_english(text: str) -> None:
    from language.detect import detect_language

    assert detect_language(text).code == "en-IN"

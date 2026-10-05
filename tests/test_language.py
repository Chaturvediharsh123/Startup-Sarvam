"""Tests for script detection, language mapping and canned phrases."""

from __future__ import annotations

import pytest

from language.detect import (
    TTS_LANGUAGES,
    detect_language,
    detect_script,
    normalise_code,
    to_realtime_stt_code,
    to_tts_language,
)
from language.phrases import PHRASES, phrase


@pytest.mark.parametrize(
    "text, script, code",
    [
        ("Open Chrome", "latin", "en-IN"),
        ("Chrome kholo", "latin", "hi-IN"),
        ("Volume 30 kar do", "latin", "hi-IN"),
        ("क्रोम खोलो", "devanagari", "hi-IN"),
        ("ক্রোম খোলো", "bengali", "bn-IN"),
        ("ਕ੍ਰੋਮ ਖੋਲੋ", "gurmukhi", "pa-IN"),
        ("ક્રોમ ખોલો", "gujarati", "gu-IN"),
        ("କ୍ରୋମ ଖୋଲ", "oriya", "od-IN"),
        ("குரோம் திற", "tamil", "ta-IN"),
        ("క్రోమ్ తెరువు", "telugu", "te-IN"),
        ("ಕ್ರೋಮ್ ತೆರೆ", "kannada", "kn-IN"),
        ("ക്രോം തുറക്കൂ", "malayalam", "ml-IN"),
        ("کروم کھولو", "arabic", "ur-IN"),
    ],
)
def test_detect_script_and_language(text: str, script: str, code: str) -> None:
    assert detect_script(text) == script
    info = detect_language(text)
    assert info.code == code
    assert info.script == script


def test_mixed_text_uses_majority_script() -> None:
    assert detect_script("Chrome खोलो जल्दी से") == "devanagari"


def test_romanised_flag() -> None:
    assert detect_language("Chrome kholo").romanised is True
    assert detect_language("Open Chrome").romanised is False
    assert detect_language("क्रोम खोलो").romanised is False


def test_stt_language_wins() -> None:
    assert detect_language("Chrome kholo", stt_language="mr-IN").code == "mr-IN"
    assert detect_language("hello", stt_language="unknown").code == "en-IN"
    assert detect_language("ଖୋଲ", stt_language="or-IN").code == "od-IN"


def test_empty_text_uses_default() -> None:
    info = detect_language("  123 !! ", default="ta-IN")
    assert info.code == "ta-IN"
    assert info.script == "unknown"


def test_normalise_code() -> None:
    assert normalise_code(None) is None
    assert normalise_code("auto") is None
    assert normalise_code("HI-in") == "hi-IN"
    assert normalise_code("or-IN") == "od-IN"


def test_tts_language_mapping() -> None:
    for code in TTS_LANGUAGES:
        assert to_tts_language(code) == code
    assert to_tts_language("or-IN") == "od-IN"
    assert to_tts_language("ur-IN") == "hi-IN"
    assert to_tts_language("as-IN") == "bn-IN"
    assert to_tts_language("sat-IN") == "en-IN"
    assert to_tts_language(None) == "en-IN"


def test_realtime_code() -> None:
    assert to_realtime_stt_code("od-IN") == "or-IN"
    assert to_realtime_stt_code("hi-IN") == "hi-IN"


def test_phrases_fallbacks() -> None:
    assert phrase("done", "hi-IN") == "हो गया।"
    assert phrase("done", "hi-IN", romanised=True) == "Ho gaya."
    assert phrase("done", "ta-IN") == "முடிந்தது."
    assert phrase("done", "gu-IN") == "Done."
    assert phrase("done", "ta-IN", romanised=True) == "Done."
    close = phrase("confirm_close_app", "en-IN", name="Chrome")
    assert close == "Should I close Chrome? Say yes or no."
    assert phrase("confirm_generic", "ta-IN", name="x").startswith("Should I")


def test_every_phrase_has_english() -> None:
    for table in PHRASES.values():
        assert "en" in table

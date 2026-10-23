from language.pipeline import process_language
from language.types import LanguageInput


def test_hinglish_pipeline():
    result = process_language(
        LanguageInput(text="  Chrome   kholo  ")
    )

    assert result.original_text == "  Chrome   kholo  "
    assert result.normalized_text == "Chrome kholo"
    assert result.language == "hi-en"
    assert result.script == "latin"


def test_hindi_pipeline():
    result = process_language(
        LanguageInput(text="क्रोम खोलो")
    )

    assert result.language == "hi"
    assert result.script == "devanagari"


def test_english_pipeline():
    result = process_language(
        LanguageInput(text="Open Chrome")
    )

    assert result.language == "en"
    assert result.script == "latin"


def test_confidence_is_preserved():
    result = process_language(
        LanguageInput(
            text="Open Chrome",
            confidence=0.94,
        )
    )

    assert result.confidence == 0.94

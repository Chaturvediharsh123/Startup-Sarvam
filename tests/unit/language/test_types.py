from language.types import LanguageInput, LanguageResult


def test_language_input():
    value = LanguageInput(
        text="Chrome kholo"
    )

    assert value.text == "Chrome kholo"


def test_language_result():
    result = LanguageResult(
        original_text="Chrome kholo",
        normalized_text="Chrome kholo",
        language="hi-en",
        script="latin",
    )

    assert result.language == "hi-en"
    assert result.script == "latin"

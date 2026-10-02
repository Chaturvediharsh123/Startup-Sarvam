from language.detector import detect_language, detect_script
from language.normalizer import normalize_text
from language.types import LanguageInput, LanguageResult


def process_language(value: LanguageInput) -> LanguageResult:
    """
    Process raw STT text through the language layer.

    The original transcript is preserved.
    The normalized transcript is passed downstream.
    """

    normalized_text = normalize_text(value.text)

    language = detect_language(normalized_text)
    script = detect_script(normalized_text)

    return LanguageResult(
        original_text=value.text,
        normalized_text=normalized_text,
        language=language,
        script=script,
        confidence=value.confidence,
    )
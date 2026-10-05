"""Optional text-based wake word.

When ``WAKE_WORD`` is set, only transcripts that start with it are acted on, and
the wake word is removed before the text reaches the brain. Matching covers the
romanised word and its common native-script spellings, and allows a short
greeting in front ("hey sarvam", "ok सर्वम").
"""

from __future__ import annotations

import unicodedata

# Native-script spellings of known wake words; add more as needed.
NATIVE_FORMS: dict[str, tuple[str, ...]] = {
    "sarvam": ("सर्वम", "सर्वम्", "सरवम", "ஸர்வம்", "சர்வம்", "సర్వం", "ಸರ್ವಂ", "সর্বম", "സർവം"),
}
GREETINGS = frozenset(
    {"hey", "hi", "hello", "ok", "okay", "o", "arre", "are", "हे", "ओके", "हाय", "अरे"}
)


def _words(text: str) -> list[str]:
    """Lower-case words with punctuation removed (Indic vowel signs kept)."""
    norm = unicodedata.normalize("NFC", text).lower()
    return "".join(" " if unicodedata.category(c)[0] in ("P", "S") else c for c in norm).split()


def wake_variants(wake_word: str) -> set[str]:
    """Every accepted spelling. ``WAKE_WORD`` may list several, comma-separated."""
    variants: set[str] = set()
    for item in wake_word.split(","):
        word = " ".join(_words(item))
        if word:
            variants.add(word)
            variants.update(NATIVE_FORMS.get(word, ()))
    return variants


def strip_wake_word(text: str, wake_word: str) -> str | None:
    """Return the command after the wake word, or ``None`` if it is missing.

    With no wake word configured, ``text`` is returned unchanged. When the user
    says only the wake word, an empty string is returned.
    """
    if not wake_word.strip():
        return text
    words = _words(text)
    skip = 0
    while skip < len(words) and words[skip] in GREETINGS:
        skip += 1
    for variant in sorted(wake_variants(wake_word), key=len, reverse=True):
        parts = variant.split()
        if words[skip : skip + len(parts)] == parts:
            return _remainder(text, skip + len(parts))
    return None


def _remainder(text: str, word_count: int) -> str:
    """Original text after the first ``word_count`` words (keeps case and punctuation)."""
    tokens = text.split()
    seen = 0
    for index, token in enumerate(tokens):
        seen += len(_words(token))
        if seen >= word_count:
            return " ".join(tokens[index + 1 :]).lstrip(" ,.!:;-–—।")
    return ""

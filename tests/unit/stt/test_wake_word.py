"""Tests for the optional text-level wake word filter."""

from __future__ import annotations

import pytest

from stt.wake_word import strip_wake_word, wake_variants


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Sarvam, notepad kholo", "notepad kholo"),
        ("hey sarvam Volume 30 kar do", "Volume 30 kar do"),
        ("सर्वम नोटपैड खोलो", "नोटपैड खोलो"),
        ("ok सर्वम्, क्रोम खोलो।", "क्रोम खोलो।"),
        ("Sarvam", ""),
        ("notepad kholo", None),
        ("the sarvam app", None),
    ],
)
def test_wake_word(text: str, expected: str | None) -> None:
    assert strip_wake_word(text, "sarvam") == expected


def test_wake_word_off_passes_through() -> None:
    assert strip_wake_word("Notepad kholo", "") == "Notepad kholo"


def test_wake_variants_custom_list() -> None:
    variants = wake_variants("Dost, दोस्त")
    assert {"dost", "दोस्त"} <= variants
    assert strip_wake_word("दोस्त गाना चलाओ", "dost,दोस्त") == "गाना चलाओ"

"""Tests for the TTS text cleaner: markup, symbols, numbers, English inside Hindi."""

from __future__ import annotations

import pytest

from tts.text_cleaner import (
    clean_for_speech,
    number_to_words,
    speech_style,
    strip_markup,
)


@pytest.mark.parametrize(
    "number, en, hi, roman",
    [
        (0, "zero", "शून्य", "shunya"),
        (7, "seven", "सात", "saat"),
        (15, "fifteen", "पंद्रह", "pandrah"),
        (21, "twenty one", "इक्कीस", "ikkees"),
        (40, "forty", "चालीस", "chaalees"),
        (59, "fifty nine", "उनसठ", "unsath"),
        (99, "ninety nine", "निन्यानवे", "ninyaanve"),
        (100, "one hundred", "सौ", "sau"),
        (250, "two hundred fifty", "दो सौ पचास", "do sau pachaas"),
        (2025, "two thousand twenty five", "दो हज़ार पच्चीस", "do hazaar pachchees"),
        (150000, "one lakh fifty thousand", "एक लाख पचास हज़ार", "ek laakh pachaas hazaar"),
        (30000000, "three crore", "तीन करोड़", "teen crore"),
    ],
)
def test_number_to_words(number: int, en: str, hi: str, roman: str) -> None:
    assert number_to_words(number, "en") == en
    assert number_to_words(number, "hi") == hi
    assert number_to_words(number, "hi_roman") == roman


def test_every_small_number_has_a_word() -> None:
    for style in ("en", "hi", "hi_roman"):
        words = [number_to_words(n, style) for n in range(101)]
        assert all(words) and len(set(words)) == 101


@pytest.mark.parametrize(
    "text, language, expected",
    [
        ("नमस्ते दोस्त", "hi-IN", "hi"),
        ("Volume kam kar diya", "hi-IN", "hi_roman"),
        ("PC का CPU तेज़ है", "hi-IN", "hi"),
        ("The volume is low", "en-IN", "en"),
        ("வணக்கம் 10", "ta-IN", "other"),
        ("नमस्ते", None, "hi"),
    ],
)
def test_speech_style(text: str, language: str | None, expected: str) -> None:
    assert speech_style(text, language) == expected


@pytest.mark.parametrize(
    "text, language, expected",
    [
        # Devanagari Hindi: numbers, percent, currency, time, temperature in Hindi words
        ("वॉल्यूम 40% कर दिया।", "hi-IN", "वॉल्यूम चालीस प्रतिशत कर दिया।"),
        ("कीमत ₹2,500 है", "hi-IN", "कीमत दो हज़ार पाँच सौ रुपये है"),
        ("अभी 10:30 बजे हैं", "hi-IN", "अभी दस बजकर तीस मिनट हैं"),
        ("मीटिंग 11:00 बजे है", "hi-IN", "मीटिंग ग्यारह बजे है"),
        ("तापमान 32°C है", "hi-IN", "तापमान बत्तीस डिग्री सेल्सियस है"),
        ("दाम 3.5 है", "hi-IN", "दाम तीन दशमलव पाँच है"),
        # English words inside Hindi become Devanagari
        ("Notepad खुल गया", "hi-IN", "नोटपैड खुल गया"),
        ("PC और CPU ठीक हैं & RAM भी", "hi-IN", "पीसी और सीपीयू ठीक हैं और आरएएम भी"),
        # romanised Hindi
        ("Volume 40 kar diya.", "hi-IN", "Volume chaalees kar diya."),
        ("Battery 85% hai", "hi-IN", "Battery pachaasi percent hai"),
        ("Abhi 4:05 baje hain", "hi-IN", "Abhi chaar bajkar paanch minute hain"),
        # English
        ("It is 10:30 now", "en-IN", "It is ten thirty now"),
        ("Meeting at 9:00", "en-IN", "Meeting at nine o'clock"),
        ("Pay ₹1,500 & $20", "en-IN", "Pay one thousand five hundred rupees and twenty dollars"),
        ("Pi is 3.14", "en-IN", "Pi is three point one four"),
        ("It is -5 outside", "en-IN", "It is minus five outside"),
        ("Call 0987654", "en-IN", "Call zero nine eight seven six five four"),
        ("2 + 2 = 4", "en-IN", "two plus two equals four"),
        # digits glued to letters are left alone
        ("Play the mp3 file", "en-IN", "Play the mp3 file"),
        # other languages keep their digits
        ("மணி 10:30", "ta-IN", "மணி 10:30"),
        ("माझं वय 25 आहे", "mr-IN", "माझं वय 25 आहे"),
    ],
)
def test_clean_for_speech(text: str, language: str, expected: str) -> None:
    assert clean_for_speech(text, language) == expected


def test_devanagari_digits_are_spoken() -> None:
    assert clean_for_speech("वॉल्यूम ४० है", "hi-IN") == "वॉल्यूम चालीस है"


def test_strip_markup() -> None:
    text = (
        "# Result\n"
        "**Done!** Opened [Chrome](https://google.com) 😀👍🏽\n"
        "- item *one*\n"
        "1. step one\n"
        "See https://example.com/page?x=1 or www.sarvam.ai\n"
        "```python\nprint('hi')\n```\n"
        "Use `notepad` -> save"
    )
    cleaned = clean_for_speech(text, "en-IN")
    assert cleaned.splitlines() == [
        "Result",
        "Done! Opened Chrome",
        "item one",
        "one, step one",
        "See or",
        "Use notepad, save",
    ]
    for gone in ("http", "www", "*", "#", "`", "😀", "print"):
        assert gone not in cleaned


def test_emoji_removal_keeps_indic_text() -> None:
    assert strip_markup("नमस्ते 👋🏽 दोस्त ❤️").split() == ["नमस्ते", "दोस्त"]
    assert clean_for_speech("க்ஷ ✅", "ta-IN") == "க்ஷ"

import pytest

from language.detector import detect_language, detect_script


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Open Chrome", "en"),
        ("What is my battery level?", "en"),
        ("क्रोम खोलो", "hi"),
        ("बैटरी कितनी है?", "hi"),
        ("Chrome kholo", "hi-en"),
        ("YouTube pe music chalao", "hi-en"),
        ("Volume 40 kar do", "hi-en"),
    ],
)
def test_detect_language(text, expected):
    assert detect_language(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Open Chrome", "latin"),
        ("क्रोम खोलो", "devanagari"),
        ("Chrome kholo", "latin"),
    ],
)
def test_detect_script(text, expected):
    assert detect_script(text) == expected

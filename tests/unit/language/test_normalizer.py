import pytest

from language.normalizer import normalize_text


@pytest.mark.parametrize(
    "text, expected",
    [
        ("  Chrome kholo  ", "Chrome kholo"),
        ("Open    Chrome", "Open Chrome"),
        ("  YouTube   pe   music   chalao  ", "YouTube pe music chalao"),
        ("क्रोम   खोलो", "क्रोम खोलो"),
    ],
)
def test_normalize_text(text, expected):
    assert normalize_text(text) == expected
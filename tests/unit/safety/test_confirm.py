"""Multilingual yes/no detection and confirmation questions."""

from __future__ import annotations

import pytest

from safety.confirm import ask_yes_no, confirmation_question, is_no, is_yes


@pytest.mark.parametrize(
    "answer",
    ["haan", "Haan ji", "हाँ", "हां", "yes", "Yes please", "ok", "aam", "avunu", "ho", "சரி",
     "ஆம்", "అవును", "ಹೌದು", "হ্যাঁ", "होय", "theek hai", "haan kar do", "bilkul", "ठीक है",
     "sure, go ahead"],
)
def test_is_yes_accepts_clear_yes(answer: str) -> None:
    assert is_yes(answer) is True


@pytest.mark.parametrize(
    "answer",
    [None, "", "   ", "nahi", "नहीं", "haan nahi", "no", "nope", "mat karo", "haan?", "kya?",
     "yes no", "இல்லை", "వద్దు", "ಬೇಡ", "না", "नाही", "hmm", "uh", "wait", "kya bola",
     "haan haan haan but actually let us think about it more", "ok... ruko", "don't"],
)
def test_is_yes_rejects_everything_else(answer: str | None) -> None:
    assert is_yes(answer) is False


@pytest.mark.parametrize("answer", ["nahi", "no", "मत करो", "vendam", "వద్దు", "haan nahi", "nako"])
def test_is_no(answer: str) -> None:
    assert is_no(answer) is True


@pytest.mark.parametrize("answer", [None, "", "haan", "hmm", "yes"])
def test_is_no_false(answer: str | None) -> None:
    assert is_no(answer) is False


@pytest.mark.parametrize(
    "language, romanised, expected",
    [
        ("en-IN", False, "Should I shut down the computer? Say yes or no."),
        ("hi-IN", True, "Kya main computer band kar doon? Haan ya nahi boliye."),
        ("hi-IN", False, "क्या मैं कंप्यूटर बंद कर दूँ? हाँ या नहीं बोलिए।"),
        ("mr-IN", False, "संगणक बंद करू का? हो किंवा नाही सांगा."),
        ("xx-YY", False, "Should I shut down the computer? Say yes or no."),
        (None, False, "Should I shut down the computer? Say yes or no."),
    ],
)
def test_confirmation_question_languages(language: str | None, romanised: bool,
                                         expected: str) -> None:
    assert confirmation_question("shutdown_pc", {}, language, romanised) == expected


def test_confirmation_question_names_app_and_generic() -> None:
    assert "file manager" in confirmation_question("close_app", {"name": "file_manager"}, "en")
    assert confirmation_question("close_app", {"name": "chrome"}, "ta-IN").startswith("chrome ஐ")
    assert confirmation_question("delete_everything", None, "en") == (
        "Should I do this: delete everything? Say yes or no."
    )


def test_ask_yes_no() -> None:
    asked: list[tuple[str, float]] = []

    def listen(question: str, timeout: float) -> str | None:
        asked.append((question, timeout))
        return "haan ji"

    assert ask_yes_no(listen, "Q?", 10) is True
    assert asked == [("Q?", 10)]
    assert ask_yes_no(lambda q, t: None, "Q?") is False  # silence / timeout
    assert ask_yes_no(lambda q, t: "hmm", "Q?") is False


def test_ask_yes_no_error_is_no() -> None:
    def broken(question: str, timeout: float) -> str:
        raise RuntimeError("mic died")

    assert ask_yes_no(broken, "Q?") is False

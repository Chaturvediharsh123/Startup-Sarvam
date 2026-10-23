"""Tests for splitting replies into speakable sentences."""

from __future__ import annotations

from tts.sentence_splitter import MAX_SENTENCE_CHARS, split_sentences


def test_split_sentences() -> None:
    text = "नमस्ते। मैं आपकी मदद करूँगा। Notepad khul gaya! Kya aur kuch chahiye?"
    parts = split_sentences(text)
    assert parts[0].startswith("नमस्ते। मैं")  # short first piece merged with the next
    assert "".join(parts).replace(" ", "") == text.replace(" ", "")
    long = " ".join(["word"] * 300)
    assert all(len(p) <= 450 for p in split_sentences(long))
    assert split_sentences("   ") == []


def test_each_long_sentence_is_its_own_request() -> None:
    text = "This is the first sentence here. This is the second sentence here."
    assert split_sentences(text) == [
        "This is the first sentence here.",
        "This is the second sentence here.",
    ]


def test_newlines_split() -> None:
    assert split_sentences("Pehli line kaafi lambi hai yahan\nDoosri line bhi lambi hai") == [
        "Pehli line kaafi lambi hai yahan",
        "Doosri line bhi lambi hai",
    ]


def test_long_piece_prefers_comma() -> None:
    clause = "a" * 250
    text = f"{clause}, {'b ' * 150}"
    parts = split_sentences(text, max_chars=MAX_SENTENCE_CHARS)
    assert parts[0] == f"{clause},"
    assert all(len(p) <= MAX_SENTENCE_CHARS for p in parts)


def test_unbreakable_text_is_hard_cut() -> None:
    parts = split_sentences("x" * 1000, max_chars=400)
    assert [len(p) for p in parts] == [400, 400, 200]

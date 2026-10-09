"""Spoken confirmation for high-risk actions (PDF "Safety": confirmation flow, yes/no).

Only a clear yes counts ("haan", "haan ji", "yes", "aam", "avunu", "ho", ...).
Anything else - a no word anywhere, a question, silence, a long rambling answer,
an error, or the 10-second timeout - is treated as no.
"""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Callable
from typing import Any

from language.phrases import NO_WORDS, PHRASES, QUESTION_WORDS, YES_PHRASES, YES_WORDS, phrase

logger = logging.getLogger(__name__)

AskUser = Callable[[str], str | None]
"""Speaks a question and returns the user's answer text (or ``None`` for silence)."""

Listen = Callable[[str, float], str | None]
"""Speaks a question and waits up to the given seconds for an answer."""

MAX_YES_TOKENS = 8

# Used only if the shared phrase table lacks a key (language/phrases.py is owned by WS1).
_FALLBACK = {
    "confirm_close_app": "Should I close {name}? Say yes or no.",
    "confirm_lock_pc": "Should I lock the computer? Say yes or no.",
    "confirm_shutdown_pc": "Should I shut down the computer? Say yes or no.",
    "confirm_generic": "Should I do this: {name}? Say yes or no.",
}


def _tokens(text: str) -> list[str]:
    """Lower-case words with punctuation and symbols removed (keeps Indic vowel signs)."""
    norm = unicodedata.normalize("NFC", text).lower().replace("’", "'")
    cleaned = "".join(
        " " if c != "'" and unicodedata.category(c)[0] in ("P", "S") else c for c in norm
    )
    return cleaned.split()


def is_no(text: str | None) -> bool:
    """True when the answer contains any "no" word ("nahi", "no", "vendam", "ಬೇಡ", ...)."""
    if not text or not text.strip():
        return False
    return any(t in NO_WORDS for t in _tokens(text))


def is_yes(text: str | None) -> bool:
    """True only for a clear "yes" in a supported language.

    Any "no" word anywhere, silence, ``None``, noise, a question, or a long
    rambling answer counts as False.
    """
    if not text or not text.strip():
        return False
    if "?" in text or "？" in text:
        return False
    tokens = _tokens(text)
    if not tokens or len(tokens) > MAX_YES_TOKENS:
        return False
    if any(t in NO_WORDS or t in QUESTION_WORDS for t in tokens):
        return False
    joined = f" {' '.join(tokens)} "
    if any(f" {p} " in joined for p in YES_PHRASES):
        return True
    return any(t in YES_WORDS for t in tokens)


def _say(key: str, language: str | None, romanised: bool, **values: Any) -> str:
    if key in PHRASES:
        try:
            return phrase(key, language, romanised, **values)
        except (KeyError, IndexError, ValueError):
            logger.warning("Phrase %s is broken; using the English fallback", key)
    return _FALLBACK[key].format(**values)


def confirmation_question(
    name: str, args: dict[str, Any] | None, language: str | None, romanised: bool = False
) -> str:
    """The spoken question asked before a high-risk action, in the user's language/script."""
    args = args or {}
    key = f"confirm_{name}"
    if key in PHRASES or key in _FALLBACK:
        target = str(args.get("name", "")).replace("_", " ")
        return _say(key, language, romanised, name=target)
    return _say("confirm_generic", language, romanised, name=name.replace("_", " "))


def ask_yes_no(listen: Listen, question: str, timeout_s: float = 10.0) -> bool:
    """Ask ``question`` and wait up to ``timeout_s``; True only for a clear yes.

    Silence, a timeout, an unclear answer or any error counts as no.
    """
    try:
        answer = listen(question, timeout_s)
    except Exception:  # noqa: BLE001 - a broken prompt must count as "no"
        logger.exception("Asking for confirmation failed; treating as no")
        return False
    logger.info("Confirmation answer: %r", answer)
    return is_yes(answer)

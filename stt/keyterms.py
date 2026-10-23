"""Deepgram keyterm prompting: words to listen for harder (app names, names of people).

Deepgram ``nova-3`` boosts the terms sent as repeated ``keyterm=`` query parameters.
It accepts at most 500 tokens across all terms and recommends 20-50 terms. Badly
formatted terms (commas, ``term:weight``) are not rejected, they just boost nothing,
so :func:`clean_keyterms` removes them before they are sent.

https://developers.deepgram.com/docs/keyterm
"""

from __future__ import annotations

from collections.abc import Iterable

MAX_KEYTERMS = 50
MAX_KEYTERM_CHARS = 40


def clean_keyterms(terms: Iterable[str], limit: int = MAX_KEYTERMS) -> list[str]:
    """Trimmed, de-duplicated (case-insensitive) terms in their original order.

    Drops empty terms, terms over :data:`MAX_KEYTERM_CHARS` characters and terms with
    ``,`` ``;`` ``:`` or line breaks, which Deepgram would treat as one literal term.
    Keeps at most ``limit`` terms.
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in terms:
        term = " ".join(str(raw).split())
        if not term or len(term) > MAX_KEYTERM_CHARS or any(c in term for c in ",;:"):
            continue
        if term.casefold() in seen:
            continue
        seen.add(term.casefold())
        cleaned.append(term)
        if len(cleaned) >= limit:
            break
    return cleaned


def keyterm_params(terms: Iterable[str]) -> list[tuple[str, str]]:
    """``[("keyterm", term), ...]`` for a query string (one parameter per term)."""
    return [("keyterm", term) for term in clean_keyterms(terms)]

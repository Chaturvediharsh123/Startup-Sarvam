"""Split a reply into speakable sentences for sentence-level TTS pipelining.

Each piece becomes one TTS request, so sentence 1 can play while sentence 2 is
being generated. Very short pieces are merged with the next one (fewer requests,
more natural prosody); pieces longer than ``max_chars`` are cut at the last comma
or space before the limit.
"""

from __future__ import annotations

import re

MAX_SENTENCE_CHARS = 450
MIN_SENTENCE_CHARS = 25
_SENTENCE_END = re.compile(r"(?<=[.!?।॥])\s+|\n+")
_SOFT_BREAKS = (", ", "، ", "; ", ": ")


def _cut_point(piece: str, max_chars: int) -> int:
    """Where to cut an over-long piece: after a comma if one is reasonably late, else a space."""
    best = max(piece.rfind(mark, 0, max_chars) for mark in _SOFT_BREAKS)
    if best >= max_chars // 2:
        return best + 1
    cut = piece.rfind(" ", 0, max_chars)
    return cut if cut > 0 else max_chars


def split_sentences(text: str, max_chars: int = MAX_SENTENCE_CHARS) -> list[str]:
    """Split ``text`` into speakable pieces.

    Sentences end at ``. ! ? । ॥`` followed by whitespace, or at a line break.
    Pieces shorter than :data:`MIN_SENTENCE_CHARS` are merged with the next one.
    """
    raw = [p.strip() for p in _SENTENCE_END.split(text) if p and p.strip()]
    merged: list[str] = []
    for piece in raw:
        if merged and len(merged[-1]) < MIN_SENTENCE_CHARS:
            merged[-1] = f"{merged[-1]} {piece}"
        else:
            merged.append(piece)
    out: list[str] = []
    for piece in merged:
        while len(piece) > max_chars:
            cut = _cut_point(piece, max_chars)
            out.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if piece:
            out.append(piece)
    return out

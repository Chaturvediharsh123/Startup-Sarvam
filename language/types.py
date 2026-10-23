"""Data types for the V1 Hindi/English language pipeline (internal; see ``language_spec.md``).

Public language detection lives in :mod:`language.detect` (``LanguageInfo``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Language = Literal["en", "hi", "hi-en"]
Script = Literal["latin", "devanagari", "mixed", "unknown"]


@dataclass
class LanguageInput:
    """
    Raw input received from the speech-to-text layer.
    """

    text: str
    confidence: float | None = None


@dataclass
class LanguageResult:
    """
    Structured result produced by the language layer.
    """

    original_text: str
    normalized_text: str

    language: Language
    script: Script

    confidence: float | None = None

from dataclasses import dataclass
from typing import Literal, Optional


Language = Literal["en", "hi", "hi-en"]
Script = Literal["latin", "devanagari", "mixed", "unknown"]


@dataclass
class LanguageInput:
    """
    Raw input received from the speech-to-text layer.
    """

    text: str
    confidence: Optional[float] = None


@dataclass
class LanguageResult:
    """
    Structured result produced by the language layer.
    """

    original_text: str
    normalized_text: str

    language: Language
    script: Script

    confidence: Optional[float] = None
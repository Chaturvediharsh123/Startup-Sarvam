"""Language layer: script/language detection and code mapping.

:mod:`language.detect` is the single public API. Import from here or from
``language.detect``::

    from language import LanguageInfo, detect_language

``language.detector`` / ``language.pipeline`` / ``language.normalizer`` /
``language.types`` hold the V1 Hindi/English word-list helper from
``language_spec.md``; they are internal and used by ``detect`` to recognise
Hinglish written in Roman letters. Canned reply phrases live in
``language.phrases``.
"""

from __future__ import annotations

from language.detect import (
    SCRIPT_RANGES,
    TTS_LANGUAGES,
    LanguageInfo,
    detect_language,
    detect_script,
    normalise_code,
    to_realtime_stt_code,
    to_tts_language,
)

__all__ = [
    "SCRIPT_RANGES",
    "TTS_LANGUAGES",
    "LanguageInfo",
    "detect_language",
    "detect_script",
    "normalise_code",
    "to_realtime_stt_code",
    "to_tts_language",
]

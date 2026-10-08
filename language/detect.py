"""Script and language detection for transcripts and typed text.

This is the single public language API (re-exported by :mod:`language`). Brain,
STT and TTS import ``detect_language``, ``detect_script``, ``LanguageInfo``,
``normalise_code``, ``to_tts_language`` and ``to_realtime_stt_code`` from here.
:mod:`language.detector` is an internal Hinglish word-list helper.

Detects the writing script from Unicode ranges and maps it to a Sarvam BCP-47
code. Romanised Hindi (Hinglish) maps to ``hi-IN`` using the vocabulary in
:mod:`language.detector`. A language code returned by speech-to-text always wins
over guessing from the text.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from language.detector import detect_language as detect_v1_language

# (first code point, last code point, script name, Sarvam language code)
SCRIPT_RANGES: tuple[tuple[int, int, str, str], ...] = (
    (0x0900, 0x097F, "devanagari", "hi-IN"),
    (0x0980, 0x09FF, "bengali", "bn-IN"),
    (0x0A00, 0x0A7F, "gurmukhi", "pa-IN"),
    (0x0A80, 0x0AFF, "gujarati", "gu-IN"),
    (0x0B00, 0x0B7F, "oriya", "od-IN"),
    (0x0B80, 0x0BFF, "tamil", "ta-IN"),
    (0x0C00, 0x0C7F, "telugu", "te-IN"),
    (0x0C80, 0x0CFF, "kannada", "kn-IN"),
    (0x0D00, 0x0D7F, "malayalam", "ml-IN"),
    (0x0600, 0x06FF, "arabic", "ur-IN"),
)

# The 11 languages Bulbul text-to-speech accepts (docs/sarvam_api_notes.md, section 5).
TTS_LANGUAGES = frozenset(
    {
        "bn-IN", "en-IN", "gu-IN", "hi-IN", "kn-IN", "ml-IN",
        "mr-IN", "od-IN", "pa-IN", "ta-IN", "te-IN",
    }
)
# Languages Bulbul cannot speak that are close enough to Hindi to read Devanagari/Hindi aloud.
_TTS_HINDI_FALLBACK = frozenset({"ur-IN", "mai-IN", "ne-IN", "doi-IN", "kok-IN", "sa-IN", "brx-IN"})
_NO_LANGUAGE = frozenset({"", "unknown", "auto", "none", "null"})


@dataclass(frozen=True)
class LanguageInfo:
    """Result of language detection.

    Attributes:
        code: Sarvam BCP-47 code such as ``hi-IN``.
        script: dominant script name (``latin``, ``devanagari``, ``tamil``, …, or ``unknown``).
        romanised: True when an Indian language is written in Latin letters (Hinglish).
    """

    code: str
    script: str
    romanised: bool


def normalise_code(code: str | None) -> str | None:
    """Clean a language code from STT or config. Returns ``None`` when there is none.

    Odia is ``or-IN`` on realtime STT but ``od-IN`` everywhere else; we use ``od-IN``.
    """
    if code is None:
        return None
    value = code.strip()
    if value.lower() in _NO_LANGUAGE:
        return None
    if value.lower() in ("or-in", "od-in"):
        return "od-IN"
    parts = value.split("-")
    if len(parts) == 2:
        return f"{parts[0].lower()}-{parts[1].upper()}"
    return value


def detect_script(text: str) -> str:
    """Return the script that most letters in ``text`` use."""
    counts: Counter[str] = Counter()
    for char in text:
        point = ord(char)
        if char.isascii():
            if char.isalpha():
                counts["latin"] += 1
            continue
        for low, high, name, _ in SCRIPT_RANGES:
            if low <= point <= high:
                counts[name] += 1
                break
    if not counts:
        return "unknown"
    return counts.most_common(1)[0][0]


def _code_for_script(script: str) -> str | None:
    for _, _, name, code in SCRIPT_RANGES:
        if name == script:
            return code
    return None


# Common Roman-script Hindi words missing from the V1 list in ``detector.py`` that
# show up in short voice commands and answers ("sab bhool jao", "ruko", "haan ji").
EXTRA_HINGLISH_WORDS = frozenset({
    "haan", "haanji", "ji", "nahi", "nahin", "ruko", "rukiye", "ruk", "jao", "jaao",
    "bhool", "bhul", "sab", "kuch", "kya", "kaise", "kitna", "kitni", "kitne", "batao",
    "bataiye", "dikhao", "chalao", "suno", "sunao", "aur", "bhi", "abhi", "isko", "usko",
    "ise", "mujhe", "mera", "meri", "aap", "theek", "accha", "achha", "pakka",
    "bas", "kam", "zyada", "jyada", "upar", "neeche", "niche",
})


def _looks_hinglish(text: str) -> bool:
    words = {w.strip(".,!?;:'\"").lower() for w in text.split()}
    return bool(words & EXTRA_HINGLISH_WORDS)


def detect_language(
    text: str, stt_language: str | None = None, default: str = "hi-IN"
) -> LanguageInfo:
    """Work out the language and script of ``text``.

    Args:
        text: transcript or typed command.
        stt_language: code returned by speech-to-text, preferred when present.
        default: code used when nothing can be detected.
    """
    script = detect_script(text)
    code = normalise_code(stt_language)
    if code is None:
        if script == "latin":
            hinglish = detect_v1_language(text) == "hi-en" or _looks_hinglish(text)
            code = "hi-IN" if hinglish else "en-IN"
        else:
            code = _code_for_script(script) or default
    romanised = script == "latin" and code != "en-IN"
    return LanguageInfo(code=code, script=script, romanised=romanised)


def to_tts_language(code: str | None) -> str:
    """Map any language code to one Bulbul TTS can speak."""
    norm = normalise_code(code) or "en-IN"
    if norm in TTS_LANGUAGES:
        return norm
    if norm == "as-IN":
        return "bn-IN"
    if norm in _TTS_HINDI_FALLBACK:
        return "hi-IN"
    return "en-IN"


def to_realtime_stt_code(code: str) -> str:
    """Convert a code for the realtime STT WebSocket, which spells Odia ``or-IN``."""
    return "or-IN" if normalise_code(code) == "od-IN" else code

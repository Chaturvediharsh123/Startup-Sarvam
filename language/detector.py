import re

from language.types import Language, Script


DEVANAGARI_PATTERN = re.compile(r"[\u0900-\u097F]")


# Small V1 vocabulary.
# This is intentionally conservative and will grow based on test data.
HINDI_WORDS = {
    "hai",
    "hain",
    "ho",
    "karo",
    "kar",
    "karna",
    "kar do",
    "khol",
    "kholo",
    "kholo",
    "band",
    "bando",
    "band karo",
    "lo",
    "le",
    "le lo",
    "likho",
    "likh",
    "mein",
    "me",
    "pe",
    "par",
    "ka",
    "ki",
    "ke",
    "ko",
    "se",
    "mujhe",
    "mera",
    "meri",
    "kitna",
    "kitni",
    "kya",
    "chahiye",
    "do",
    "dena",
    "batao",
    "bata",
    "chalao",
    "lagao",
}


def detect_script(text: str) -> Script:
    """
    Detect the writing script used by the transcript.
    """

    has_devanagari = bool(DEVANAGARI_PATTERN.search(text))

    latin_chars = re.findall(r"[A-Za-z]", text)
    has_latin = bool(latin_chars)

    if has_devanagari and has_latin:
        return "mixed"

    if has_devanagari:
        return "devanagari"

    if has_latin:
        return "latin"

    return "unknown"


def _contains_hindi_latin(text: str) -> bool:
    """
    Detect common Hindi words written using Latin characters.
    """

    words = set(re.findall(r"[a-zA-Z]+", text.lower()))

    return bool(words.intersection(HINDI_WORDS))


def detect_language(text: str) -> Language:
    """
    Detect the V1 interaction language.

    Returns:
        en     -> English
        hi     -> Hindi
        hi-en  -> Hindi-English mixed
    """

    text = text.strip()

    if not text:
        return "en"

    script = detect_script(text)

    if script == "devanagari":
        return "hi"

    if script == "mixed":
        return "hi-en"

    if _contains_hindi_latin(text):
        return "hi-en"

    return "en"
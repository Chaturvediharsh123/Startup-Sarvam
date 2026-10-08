"""Make reply text sound right when spoken (WS6 "text cleaner").

:func:`clean_for_speech` runs before every TTS request:

1. drops what should never be read aloud: markdown markup, code blocks, URLs, emoji;
2. expands symbols: ``%``, ``₹``/``Rs``, ``$``, ``°C``/``°F``, ``&``, ``+``, ``=``, ``@``;
3. turns numbers, decimals and clock times (``10:30``) into words for Hindi and
   English. Hindi in Devanagari gets Devanagari words (``40`` -> ``चालीस``);
   romanised Hindi (Hinglish) gets romanised words (``chaalees``); English gets
   English words with the Indian system (lakh, crore). Other languages keep digits,
   which Bulbul reads natively;
4. in Devanagari Hindi, common English words and acronyms are written in
   Devanagari (``Notepad`` -> ``नोटपैड``, ``PC`` -> ``पीसी``) so the Hindi voice
   pronounces them naturally. Unknown English words are left alone.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from language.detect import detect_language, detect_script, normalise_code

# -- number words -------------------------------------------------------------------


def _split(words: str) -> tuple[str, ...]:
    """Whitespace-separated words as a tuple (keeps the long tables readable)."""
    return tuple(words.split())


_HI_SMALL = _split(
    """शून्य एक दो तीन चार पाँच छह सात आठ नौ दस ग्यारह बारह तेरह चौदह पंद्रह सोलह सत्रह
    अठारह उन्नीस बीस इक्कीस बाईस तेईस चौबीस पच्चीस छब्बीस सत्ताईस अट्ठाईस उनतीस तीस इकतीस
    बत्तीस तैंतीस चौंतीस पैंतीस छत्तीस सैंतीस अड़तीस उनतालीस चालीस इकतालीस बयालीस तैंतालीस
    चवालीस पैंतालीस छियालीस सैंतालीस अड़तालीस उनचास पचास इक्यावन बावन तिरपन चौवन पचपन छप्पन
    सत्तावन अट्ठावन उनसठ साठ इकसठ बासठ तिरसठ चौंसठ पैंसठ छियासठ सड़सठ अड़सठ उनहत्तर सत्तर
    इकहत्तर बहत्तर तिहत्तर चौहत्तर पचहत्तर छिहत्तर सतहत्तर अठहत्तर उनासी अस्सी इक्यासी बयासी
    तिरासी चौरासी पचासी छियासी सत्तासी अट्ठासी नवासी नब्बे इक्यानवे बानवे तिरानवे चौरानवे
    पचानवे छियानवे सत्तानवे अट्ठानवे निन्यानवे सौ"""
)

_HI_ROMAN_SMALL = _split(
    """shunya ek do teen chaar paanch chhah saat aath nau das gyaarah baarah terah chaudah
    pandrah solah satrah athaarah unnees bees ikkees baaees teyees chaubees pachchees
    chhabbees sattaaees atthaaees untees tees ikattees battees taintees chauntees paintees
    chhattees saintees adtees untaalees chaalees iktaalees bayaalees taintaalees chavaalees
    paintaalees chhiyaalees saintaalees adtaalees unchaas pachaas ikyaavan baavan tirpan
    chauvan pachpan chhappan sattaavan atthaavan unsath saath iksath baasath tirsath
    chaunsath painsath chhiyaasath sadsath adsath unhattar sattar ikhattar bahattar tihattar
    chauhattar pachhattar chhihattar sathattar athhattar unaasi assi ikyaasi bayaasi tiraasi
    chauraasi pachaasi chhiyaasi sattaasi atthaasi navaasi nabbe ikyaanve baanve tiraanve
    chauraanve pachaanve chhiyaanve sattaanve atthaanve ninyaanve sau"""
)

_EN_ONES = _split(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen "
    "fourteen fifteen sixteen seventeen eighteen nineteen"
)
_EN_TENS = _split("_ _ twenty thirty forty fifty sixty seventy eighty ninety")
_EN_SMALL = tuple(
    _EN_ONES[n] if n < 20
    else _EN_TENS[n // 10] + ("" if n % 10 == 0 else f" {_EN_ONES[n % 10]}")
    for n in range(100)
) + ("one hundred",)


@dataclass(frozen=True)
class _Words:
    """Every word one speech style needs."""

    small: tuple[str, ...]  # 0..100
    hundred: str
    thousand: str
    lakh: str
    crore: str
    point: str
    minus: str
    percent: str
    rupees: str
    dollars: str
    degree: str
    celsius: str
    fahrenheit: str
    and_: str
    plus: str
    equals: str
    at: str
    time: Callable[[str, int, str], str]  # (hour words, minute, minute words)


def _en_time(hour: str, minute: int, minute_words: str) -> str:
    if minute == 0:
        return f"{hour} o'clock"
    return f"{hour} oh {minute_words}" if minute < 10 else f"{hour} {minute_words}"


def _hi_time(hour: str, minute: int, minute_words: str) -> str:
    return f"{hour} बजे" if minute == 0 else f"{hour} बजकर {minute_words} मिनट"


def _hi_roman_time(hour: str, minute: int, minute_words: str) -> str:
    return f"{hour} baje" if minute == 0 else f"{hour} bajkar {minute_words} minute"


WORDS: dict[str, _Words] = {
    "en": _Words(_EN_SMALL, "hundred", "thousand", "lakh", "crore", "point", "minus",
                 "percent", "rupees", "dollars", "degree", "Celsius", "Fahrenheit", "and",
                 "plus", "equals", "at", _en_time),
    "hi": _Words(_HI_SMALL, "सौ", "हज़ार", "लाख", "करोड़", "दशमलव", "माइनस", "प्रतिशत",
                 "रुपये", "डॉलर", "डिग्री", "सेल्सियस", "फ़ारेनहाइट", "और", "प्लस", "बराबर",
                 "एट", _hi_time),
    "hi_roman": _Words(_HI_ROMAN_SMALL, "sau", "hazaar", "laakh", "crore", "dashamlav",
                       "minus", "percent", "rupaye", "dollar", "degree", "Celsius",
                       "Fahrenheit", "aur", "plus", "barabar", "at", _hi_roman_time),
}

MAX_WORD_NUMBER = 999_999_999  # bigger numbers are read digit by digit


def number_to_words(number: int, style: str) -> str:
    """Spell a whole number (0 .. 99,99,99,999) in ``style`` (``en``/``hi``/``hi_roman``).

    Uses the Indian system: hundred, thousand, lakh, crore.
    """
    words = WORDS[style]
    if number < 0:
        return f"{words.minus} {number_to_words(-number, style)}"
    if number <= 100:
        return words.small[number]
    parts: list[str] = []
    for value, name in ((10**7, words.crore), (10**5, words.lakh), (1000, words.thousand),
                        (100, words.hundred)):
        if number >= value:
            count, number = divmod(number, value)
            parts.append(f"{number_to_words(count, style)} {name}")
    if number:
        parts.append(words.small[number])
    return " ".join(parts)


def _digits_to_words(digits: str, style: str) -> str:
    small = WORDS[style].small
    return " ".join(small[int(d)] for d in digits)


def _spell_number(sign: str, whole: str, fraction: str, style: str) -> str:
    words = WORDS[style]
    whole = whole.replace(",", "")
    if len(whole) > len(str(MAX_WORD_NUMBER)) or (len(whole) > 1 and whole.startswith("0")):
        spoken = _digits_to_words(whole, style)  # phone numbers, codes, huge values
    else:
        spoken = number_to_words(int(whole), style)
    if fraction:
        spoken += f" {words.point} {_digits_to_words(fraction, style)}"
    return f"{words.minus} {spoken}" if sign else spoken


# -- English words inside Devanagari Hindi ------------------------------------------

EN_TO_DEVANAGARI: dict[str, str] = {
    "ok": "ओके", "okay": "ओके", "hello": "हेलो", "sorry": "सॉरी", "please": "प्लीज़",
    "thanks": "थैंक्स", "notepad": "नोटपैड", "chrome": "क्रोम", "google": "गूगल",
    "youtube": "यूट्यूब", "volume": "वॉल्यूम", "battery": "बैटरी", "screenshot": "स्क्रीनशॉट",
    "file": "फ़ाइल", "files": "फ़ाइलें", "folder": "फ़ोल्डर", "settings": "सेटिंग्स",
    "setting": "सेटिंग", "wifi": "वाईफ़ाई", "wi-fi": "वाईफ़ाई", "bluetooth": "ब्लूटूथ",
    "laptop": "लैपटॉप", "computer": "कंप्यूटर", "internet": "इंटरनेट", "email": "ईमेल",
    "app": "ऐप", "apps": "ऐप्स", "mouse": "माउस", "keyboard": "कीबोर्ड",
    "download": "डाउनलोड", "calculator": "कैलकुलेटर", "paint": "पेंट", "word": "वर्ड",
    "excel": "एक्सेल", "powerpoint": "पावरपॉइंट", "camera": "कैमरा", "edge": "एज",
    "explorer": "एक्सप्लोरर", "tab": "टैब", "window": "विंडो", "windows": "विंडोज़",
    "screen": "स्क्रीन", "mute": "म्यूट", "unmute": "अनम्यूट", "shutdown": "शटडाउन",
    "restart": "रीस्टार्ट", "lock": "लॉक", "search": "सर्च", "website": "वेबसाइट",
    "online": "ऑनलाइन", "offline": "ऑफ़लाइन", "message": "मैसेज", "phone": "फ़ोन",
    "mobile": "मोबाइल", "note": "नोट", "notes": "नोट्स", "save": "सेव", "copy": "कॉपी",
    "paste": "पेस्ट", "music": "म्यूज़िक", "video": "वीडियो", "song": "सॉन्ग",
    "charge": "चार्ज", "charging": "चार्जिंग", "assistant": "असिस्टेंट", "sarvam": "सर्वम",
    "zoom": "ज़ूम", "scroll": "स्क्रॉल", "click": "क्लिक", "type": "टाइप",
    "printer": "प्रिंटर", "update": "अपडेट", "password": "पासवर्ड", "memory": "मेमोरी",
    "minute": "मिनट", "minutes": "मिनट", "second": "सेकंड", "seconds": "सेकंड",
}

LETTER_NAMES_DEVANAGARI: dict[str, str] = dict(zip(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    _split("ए बी सी डी ई एफ जी एच आई जे के एल एम एन ओ पी क्यू आर एस टी यू वी डब्ल्यू एक्स वाई ज़ेड"),
    strict=True,
))

# -- patterns -------------------------------------------------------------------------

_CODE_FENCE = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]*)`")
_IMAGE_OR_LINK = re.compile(r"!?\[([^\]\n]*)\]\([^)\n]*\)")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_QUOTE = re.compile(r"^\s*>+\s?", re.MULTILINE)
_BULLET = re.compile(r"^\s*[-*+•]\s+", re.MULTILINE)
_NUMBERED = re.compile(r"^\s*(\d+)[.)]\s+", re.MULTILINE)
_RULE = re.compile(r"^\s*[-=_*|:\s]{3,}$", re.MULTILINE)
_BOLD = re.compile(r"(\*\*|__)(.+?)\1")
_ITALIC = re.compile(r"(?<![\w*])([*_])(?!\s)(.+?)(?<!\s)\1(?![\w*])")
_LEFTOVER_MARKUP = re.compile(r"[*#_~`|<>\[\]{}^]")
_EMOJI = re.compile(
    "(?:[\U0001F000-\U0001FAFF☀-➿⌀-⏿⬀-⯿←-⇿]"
    "[️‍\U0001F3FB-\U0001F3FF]*)+"
)
_STRAY_JOINERS = re.compile("[️⃣]")
_ARROW = re.compile(r"\s*(?:->|=>|→)\s*")

_NUM = r"\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_CURRENCY_RUPEE = re.compile(rf"(?:₹|\bRs\.?|\bINR)\s*({_NUM})")
_CURRENCY_DOLLAR = re.compile(rf"\$\s*({_NUM})")
_PERCENT = re.compile(r"\s*%")
_DEGREE_C = re.compile(r"\s*°\s*C\b")
_DEGREE_F = re.compile(r"\s*°\s*F\b")
_DEGREE = re.compile(r"\s*°")
_TIME = re.compile(r"(?<![\w:.])([01]?\d|2[0-3]):([0-5]\d)(?![\w:])(\s*(?:बजे|baje))?")
_NUMBER = re.compile(r"(?<![\w.])(-?)(\d{1,3}(?:,\d{2,3})+|\d+)(?:\.(\d+))?(?!\w)")
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")
_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


def _has_devanagari(text: str) -> bool:
    return any("ऀ" <= char <= "ॿ" for char in text)


def speech_style(text: str, language: str | None) -> str:
    """Pick ``hi`` (Devanagari Hindi), ``hi_roman`` (Hinglish), ``en`` or ``other``.

    An explicit Hindi ``language`` is trusted: any Devanagari in the text means
    ``hi``, otherwise the text is romanised Hindi. Without a language, the script and
    :func:`language.detect.detect_language` decide.
    """
    code = normalise_code(language) or detect_language(text).code
    base = code.split("-")[0].lower()
    if base == "hi":
        if _has_devanagari(text):
            return "hi"
        return "hi_roman" if detect_script(text) in ("latin", "unknown") else "other"
    if base == "en":
        return "hi" if detect_script(text) == "devanagari" else "en"
    return "other"


def strip_markup(text: str) -> str:
    """Remove markdown, URLs and emoji, keeping the readable words."""
    text = _CODE_FENCE.sub(" ", text)
    text = _IMAGE_OR_LINK.sub(r"\1", text)
    text = _URL.sub(" ", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _RULE.sub("", text)
    text = _HEADING.sub("", text)
    text = _QUOTE.sub("", text)
    text = _BULLET.sub("", text)
    text = _NUMBERED.sub(r"\1, ", text)
    text = _BOLD.sub(r"\2", text)
    text = _ITALIC.sub(r"\2", text)
    text = _EMOJI.sub(" ", text)
    text = _STRAY_JOINERS.sub("", text)
    text = _ARROW.sub(", ", text)
    return text


def expand_symbols(text: str, style: str) -> str:
    """Spell out currency, percent, degree and operator symbols in ``style``."""
    w = WORDS[style]
    text = _CURRENCY_RUPEE.sub(rf"\1 {w.rupees}", text)
    text = _CURRENCY_DOLLAR.sub(rf"\1 {w.dollars}", text)
    text = _PERCENT.sub(f" {w.percent}", text)
    text = _DEGREE_C.sub(f" {w.degree} {w.celsius}", text)
    text = _DEGREE_F.sub(f" {w.degree} {w.fahrenheit}", text)
    text = _DEGREE.sub(f" {w.degree}", text)
    text = text.replace("&", f" {w.and_} ").replace("+", f" {w.plus} ")
    text = text.replace("=", f" {w.equals} ").replace("@", f" {w.at} ")
    return text


def numbers_to_words(text: str, style: str) -> str:
    """Spell clock times, decimals and whole numbers in ``style``."""
    words = WORDS[style]
    text = text.translate(_DEVANAGARI_DIGITS)

    def time(match: re.Match[str]) -> str:
        hour, minute = int(match.group(1)), int(match.group(2))
        spoken = words.time(number_to_words(hour, style), minute, number_to_words(minute, style))
        if match.group(3) and style == "en":
            spoken += match.group(3)  # "baje" after an English time is the user's word
        return spoken

    def number(match: re.Match[str]) -> str:
        return _spell_number(match.group(1), match.group(2), match.group(3) or "", style)

    return _NUMBER.sub(number, _TIME.sub(time, text))


def english_to_devanagari(text: str) -> str:
    """Write known English words and short acronyms inside Hindi text in Devanagari."""

    def replace(match: re.Match[str]) -> str:
        word = match.group(0)
        known = EN_TO_DEVANAGARI.get(word.lower())
        if known:
            return known
        if 2 <= len(word) <= 5 and word.isupper():
            return "".join(LETTER_NAMES_DEVANAGARI[c] for c in word)
        return word

    return _LATIN_WORD.sub(replace, text)


def _tidy(text: str) -> str:
    text = _LEFTOVER_MARKUP.sub(" ", text)
    lines = []
    for line in text.splitlines():
        line = re.sub(r"[ \t ]+", " ", line).strip()
        line = re.sub(r"\s+([,.!?।;:])", r"\1", line)
        line = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", line).strip(" ,;:")
        if line:
            lines.append(line)
    return "\n".join(lines)


def clean_for_speech(text: str, language: str | None) -> str:
    """Prepare ``text`` for Bulbul in ``language`` (see the module docstring)."""
    text = strip_markup(text)
    style = speech_style(text, language)
    if style != "other":
        text = expand_symbols(text, style)
        text = numbers_to_words(text, style)
        if style == "hi":
            text = english_to_devanagari(text)
    return _tidy(text)

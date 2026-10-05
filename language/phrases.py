"""Yes/no vocabularies and short canned replies in several Indian languages.

The yes/no lists drive :func:`assistant.safety.is_yes`. Canned replies are used
when the LLM gives no text or cannot be reached.

Phrase keys use the base language (``hi``, ``ta``, …). ``hi-latn`` is Hinglish
(Hindi in Roman letters).
"""

from __future__ import annotations

from typing import Any

# Single words meaning a clear "yes" (native script and romanised).
YES_WORDS: frozenset[str] = frozenset(
    {
        # English
        "yes", "yeah", "yep", "yup", "ok", "okay", "sure", "confirm", "confirmed",
        "absolutely", "definitely", "correct", "right",
        # Hindi / Hinglish
        "haan", "han", "haa", "haanji", "hanji", "ha", "ji", "jee", "theek", "thik", "bilkul",
        "zaroor", "zarur", "jaroor", "jarur", "karo", "kardo",
        "हाँ", "हां", "हा", "जी", "हांजी", "हाँजी", "ठीक", "बिल्कुल", "बिलकुल", "ज़रूर", "जरूर", "करो",
        # Marathi
        "ho", "hoy", "होय", "हो",
        # Tamil
        "aam", "aama", "aamam", "sari", "seri", "ஆம்", "ஆமா", "ஆமாம்", "சரி",
        # Telugu
        "avunu", "sare", "ouno", "అవును", "సరే", "ఔను",
        # Kannada
        "houdu", "haudu", "ಹೌದು", "ಸರಿ",
        # Bengali
        "hyan", "hya", "হ্যাঁ", "হ্যা", "হাঁ", "হুম",
    }
)

# Multi-word phrases meaning "yes".
YES_PHRASES: tuple[str, ...] = (
    "go ahead", "do it", "kar do", "kar dijiye", "kar dena", "theek hai", "thik hai",
    "कर दो", "कर दीजिए", "ठीक है", "thik ache", "ঠিক আছে", "ठीक आहे",
)

# Single words meaning "no". Any of these anywhere makes the answer "no".
NO_WORDS: frozenset[str] = frozenset(
    {
        # English
        "no", "nope", "nah", "not", "don't", "dont", "stop", "cancel", "wait", "never", "later",
        # Hindi / Hinglish
        "nahi", "nahin", "nai", "nahiin", "na", "naa", "mat", "ruko", "rukiye", "rehne",
        "नहीं", "नही", "ना", "मत", "रुको", "रुकिए", "रहने",
        # Marathi
        "nako", "नाही", "नको",
        # Tamil
        "illai", "illa", "vendam", "venam", "venda", "இல்லை", "இல்ல", "வேண்டாம்", "வேணாம்",
        # Telugu
        "kaadu", "kadu", "vaddu", "ledu", "కాదు", "వద్దు", "లేదు",
        # Kannada
        "beda", "ಇಲ್ಲ", "ಬೇಡ",
        # Bengali
        "না",
    }
)

# Words that turn the reply into a question, which never counts as "yes".
QUESTION_WORDS: frozenset[str] = frozenset(
    {
        "kya", "kyon", "kyun", "kaun", "kaise", "what", "why", "who", "how", "which",
        "क्या", "क्यों", "कौन", "कैसे", "ஏன்", "என்ன", "ఏమిటి", "ఎందుకు", "ಏನು", "ಯಾಕೆ", "কেন", "কী",
    }
)

PHRASES: dict[str, dict[str, str]] = {
    "done": {
        "en": "Done.",
        "hi": "हो गया।",
        "hi-latn": "Ho gaya.",
        "ta": "முடிந்தது.",
        "te": "అయిపోయింది.",
        "kn": "ಆಯಿತು.",
        "bn": "হয়ে গেছে।",
        "mr": "झालं.",
    },
    "denied": {
        "en": "Okay, I did not do it.",
        "hi": "ठीक है, मैंने नहीं किया।",
        "hi-latn": "Theek hai, maine nahi kiya.",
        "ta": "சரி, நான் அதைச் செய்யவில்லை.",
        "te": "సరే, నేను చేయలేదు.",
        "kn": "ಸರಿ, ನಾನು ಮಾಡಲಿಲ್ಲ.",
        "bn": "ঠিক আছে, আমি করিনি।",
        "mr": "ठीक आहे, मी केलं नाही.",
    },
    "blocked": {
        "en": "Sorry, I am not allowed to do that.",
        "hi": "माफ़ कीजिए, यह काम मैं नहीं कर सकता।",
        "hi-latn": "Maaf kijiye, yeh kaam main nahi kar sakta.",
        "ta": "மன்னிக்கவும், அதை என்னால் செய்ய முடியாது.",
        "te": "క్షమించండి, అది నేను చేయలేను.",
        "kn": "ಕ್ಷಮಿಸಿ, ಅದನ್ನು ನಾನು ಮಾಡಲು ಸಾಧ್ಯವಿಲ್ಲ.",
        "bn": "দুঃখিত, এটা আমি করতে পারব না।",
        "mr": "माफ करा, हे मी करू शकत नाही.",
    },
    "error": {
        "en": "Sorry, that did not work.",
        "hi": "माफ़ कीजिए, यह काम नहीं हो पाया।",
        "hi-latn": "Maaf kijiye, yeh kaam nahi ho paya.",
        "ta": "மன்னிக்கவும், அது வேலை செய்யவில்லை.",
        "te": "క్షమించండి, అది పని చేయలేదు.",
        "kn": "ಕ್ಷಮಿಸಿ, ಅದು ಆಗಲಿಲ್ಲ.",
        "bn": "দুঃখিত, কাজটা হল না।",
        "mr": "माफ करा, ते झालं नाही.",
    },
    "sorry_repeat": {
        "en": "Sorry, please say that again.",
        "hi": "माफ़ कीजिए, कृपया फिर से बोलिए।",
        "hi-latn": "Maaf kijiye, phir se boliye.",
        "ta": "மன்னிக்கவும், மீண்டும் சொல்லுங்கள்.",
        "te": "క్షమించండి, మళ్ళీ చెప్పండి.",
        "kn": "ಕ್ಷಮಿಸಿ, ಮತ್ತೆ ಹೇಳಿ.",
        "bn": "দুঃখিত, আবার বলুন।",
        "mr": "माफ करा, पुन्हा सांगा.",
    },
    "rate_limited": {
        "en": "Too many actions at once, please wait a moment.",
        "hi": "बहुत सारे काम एक साथ हो गए, थोड़ा रुकिए।",
        "hi-latn": "Bahut saare kaam ek saath ho gaye, thoda rukiye.",
        "ta": "ஒரே நேரத்தில் பல செயல்கள், சற்று காத்திருங்கள்.",
        "te": "ఒకేసారి చాలా పనులు, కొంచెం ఆగండి.",
        "kn": "ಒಮ್ಮೆಗೆ ತುಂಬಾ ಕೆಲಸಗಳು, ಸ್ವಲ್ಪ ಕಾಯಿರಿ.",
        "bn": "একসাথে অনেক কাজ, একটু অপেক্ষা করুন।",
        "mr": "एकाच वेळी खूप कामं, थोडं थांबा.",
    },
    "confirm_close_app": {
        "en": "Should I close {name}? Say yes or no.",
        "hi": "क्या मैं {name} बंद कर दूँ? हाँ या नहीं बोलिए।",
        "hi-latn": "Kya main {name} band kar doon? Haan ya nahi boliye.",
        "ta": "{name} ஐ மூடட்டுமா? ஆம் அல்லது இல்லை சொல்லுங்கள்.",
        "te": "{name} మూసేయమంటారా? అవును లేదా కాదు చెప్పండి.",
        "kn": "{name} ಮುಚ್ಚಲೇ? ಹೌದು ಅಥವಾ ಇಲ್ಲ ಹೇಳಿ.",
        "bn": "{name} বন্ধ করব? হ্যাঁ বা না বলুন।",
        "mr": "{name} बंद करू का? हो किंवा नाही सांगा.",
    },
    "confirm_lock_pc": {
        "en": "Should I lock the computer? Say yes or no.",
        "hi": "क्या मैं कंप्यूटर लॉक कर दूँ? हाँ या नहीं बोलिए।",
        "hi-latn": "Kya main computer lock kar doon? Haan ya nahi boliye.",
        "ta": "கணினியை லாக் செய்யட்டுமா? ஆம் அல்லது இல்லை சொல்லுங்கள்.",
        "te": "కంప్యూటర్ లాక్ చేయమంటారా? అవును లేదా కాదు చెప్పండి.",
        "kn": "ಕಂಪ್ಯೂಟರ್ ಲಾಕ್ ಮಾಡಲೇ? ಹೌದು ಅಥವಾ ಇಲ್ಲ ಹೇಳಿ.",
        "bn": "কম্পিউটার লক করব? হ্যাঁ বা না বলুন।",
        "mr": "संगणक लॉक करू का? हो किंवा नाही सांगा.",
    },
    "confirm_shutdown_pc": {
        "en": "Should I shut down the computer? Say yes or no.",
        "hi": "क्या मैं कंप्यूटर बंद कर दूँ? हाँ या नहीं बोलिए।",
        "hi-latn": "Kya main computer band kar doon? Haan ya nahi boliye.",
        "ta": "கணினியை அணைக்கட்டுமா? ஆம் அல்லது இல்லை சொல்லுங்கள்.",
        "te": "కంప్యూటర్ ఆపేయమంటారా? అవును లేదా కాదు చెప్పండి.",
        "kn": "ಕಂಪ್ಯೂಟರ್ ಆಫ್ ಮಾಡಲೇ? ಹೌದು ಅಥವಾ ಇಲ್ಲ ಹೇಳಿ.",
        "bn": "কম্পিউটার বন্ধ করব? হ্যাঁ বা না বলুন।",
        "mr": "संगणक बंद करू का? हो किंवा नाही सांगा.",
    },
    "confirm_generic": {
        "en": "Should I do this: {name}? Say yes or no.",
        "hi": "क्या मैं यह करूँ: {name}? हाँ या नहीं बोलिए।",
        "hi-latn": "Kya main yeh karoon: {name}? Haan ya nahi boliye.",
    },
}


def phrase_key(language: str | None, romanised: bool = False) -> str:
    """Choose the phrase-table key for a language code such as ``hi-IN``."""
    base = (language or "en").split("-")[0].lower()
    if romanised:
        return "hi-latn" if base == "hi" else "en"
    return base


def phrase(key: str, language: str | None, /, romanised: bool = False, **values: Any) -> str:
    """Return canned reply ``key`` in the user's language, falling back to English.

    Args:
        key: entry in :data:`PHRASES`.
        language: BCP-47 code such as ``ta-IN``.
        romanised: the user wrote/spoke in Roman letters (Hinglish).
        **values: placeholders for the template, such as ``name="Chrome"``.
    """
    table = PHRASES[key]
    lang = phrase_key(language, romanised)
    template = table.get(lang) or table.get(lang.split("-")[0]) or table["en"]
    return template.format(**values) if values else template

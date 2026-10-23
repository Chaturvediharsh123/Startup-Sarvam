"""Yes/no vocabularies and short canned replies in several Indian languages.

The yes/no lists drive :func:`safety.guard.is_yes`. Canned replies are used
when the LLM gives no text or cannot be reached, and as the fixed per-action
result templates the brain speaks after an action (``opened``, ``volume_set``, ...).
:func:`is_stop_command` recognises a spoken "ruko" / "stop" / "bas karo".

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
    # -- turn flow (orchestrator) ------------------------------------------------
    "filler": {
        "en": "One second...",
        "hi": "एक सेकंड…",
        "hi-latn": "Ek second...",
        "ta": "ஒரு நொடி...",
        "te": "ఒక్క క్షణం...",
        "kn": "ಒಂದು ಕ್ಷಣ...",
        "bn": "এক সেকেন্ড...",
        "mr": "एक सेकंद...",
    },
    "working": {
        "en": "Okay, doing it now.",
        "hi": "ठीक है, कर रहा हूँ।",
        "hi-latn": "Theek hai, kar raha hoon.",
        "ta": "சரி, செய்கிறேன்.",
        "te": "సరే, చేస్తున్నాను.",
        "kn": "ಸರಿ, ಮಾಡುತ್ತಿದ್ದೇನೆ.",
        "bn": "ঠিক আছে, করছি।",
        "mr": "ठीक आहे, करतो.",
    },
    "cancelled": {
        "en": "Okay, cancelled.",
        "hi": "ठीक है, कैंसल कर दिया।",
        "hi-latn": "Theek hai, cancel kar diya.",
        "ta": "சரி, ரத்து செய்தேன்.",
        "te": "సరే, రద్దు చేశాను.",
        "kn": "ಸರಿ, ರದ್ದು ಮಾಡಿದೆ.",
        "bn": "ঠিক আছে, বাতিল করেছি।",
        "mr": "ठीक आहे, रद्द केलं.",
    },
    "interrupted": {
        "en": "Okay, I stopped.",
        "hi": "ठीक है, रुक गया।",
        "hi-latn": "Theek hai, ruk gaya.",
        "ta": "சரி, நிறுத்திவிட்டேன்.",
        "te": "సరే, ఆపేశాను.",
        "kn": "ಸರಿ, ನಿಲ್ಲಿಸಿದೆ.",
        "bn": "ঠিক আছে, থামলাম।",
        "mr": "ठीक आहे, थांबलो.",
    },
    "timeout": {
        "en": "Sorry, that took too long. Please try again.",
        "hi": "माफ़ कीजिए, इसमें बहुत देर लग गई। फिर से कोशिश कीजिए।",
        "hi-latn": "Maaf kijiye, ismein bahut der lag gayi. Phir se koshish kijiye.",
        "ta": "மன்னிக்கவும், அதிக நேரம் ஆனது. மீண்டும் முயற்சிக்கவும்.",
        "te": "క్షమించండి, చాలా సమయం పట్టింది. మళ్ళీ ప్రయత్నించండి.",
        "kn": "ಕ್ಷಮಿಸಿ, ತುಂಬಾ ಸಮಯ ಆಯಿತು. ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ.",
        "bn": "দুঃখিত, অনেক সময় লেগে গেল। আবার চেষ্টা করুন।",
        "mr": "माफ करा, खूप वेळ लागला. पुन्हा प्रयत्न करा.",
    },
    # -- action failures -----------------------------------------------------------
    "not_installed": {
        "en": "{app} is not installed on this computer.",
        "hi": "{app} इस कंप्यूटर पर इंस्टॉल नहीं है।",
        "hi-latn": "{app} is computer par install nahi hai.",
        "ta": "{app} இந்தக் கணினியில் நிறுவப்படவில்லை.",
        "te": "{app} ఈ కంప్యూటర్‌లో ఇన్‌స్టాల్ కాలేదు.",
        "kn": "{app} ಈ ಕಂಪ್ಯೂಟರ್‌ನಲ್ಲಿ ಇನ್‌ಸ್ಟಾಲ್ ಆಗಿಲ್ಲ.",
        "bn": "{app} এই কম্পিউটারে ইনস্টল করা নেই।",
        "mr": "{app} या संगणकावर इन्स्टॉल नाही.",
    },
    "not_running": {
        "en": "{app} is not open.",
        "hi": "{app} खुला नहीं है।",
        "hi-latn": "{app} khula nahi hai.",
        "ta": "{app} திறக்கப்படவில்லை.",
        "te": "{app} తెరిచి లేదు.",
        "kn": "{app} ತೆರೆದಿಲ್ಲ.",
        "bn": "{app} খোলা নেই।",
        "mr": "{app} उघडलेलं नाही.",
    },
    # -- brain-only tools ----------------------------------------------------------
    "remembered": {
        "en": "Okay, I will remember that.",
        "hi": "ठीक है, मैंने याद रख लिया।",
        "hi-latn": "Theek hai, yaad rakh liya.",
        "ta": "சரி, நினைவில் வைத்துக்கொண்டேன்.",
        "te": "సరే, గుర్తుపెట్టుకున్నాను.",
        "kn": "ಸರಿ, ನೆನಪಿಟ್ಟುಕೊಂಡೆ.",
        "bn": "ঠিক আছে, মনে রাখলাম।",
        "mr": "ठीक आहे, लक्षात ठेवलं.",
    },
    "forgot": {
        "en": "Okay, I have forgotten our conversation.",
        "hi": "ठीक है, मैंने सब भुला दिया।",
        "hi-latn": "Theek hai, sab bhool gaya.",
        "ta": "சரி, எல்லாவற்றையும் மறந்துவிட்டேன்.",
        "te": "సరే, అంతా మర్చిపోయాను.",
        "kn": "ಸರಿ, ಎಲ್ಲವನ್ನೂ ಮರೆತೆ.",
        "bn": "ঠিক আছে, সব ভুলে গেলাম।",
        "mr": "ठीक आहे, सगळं विसरलो.",
    },
    # -- action results (used by Brain.summarise) ------------------------------------
    "opened": {
        "en": "{app} is open now.",
        "hi": "{app} खुल गया।",
        "hi-latn": "{app} khul gaya.",
        "ta": "{app} திறக்கப்பட்டது.",
        "te": "{app} తెరిచాను.",
        "kn": "{app} ತೆರೆದಿದೆ.",
        "bn": "{app} খুলে গেছে।",
        "mr": "{app} उघडलं.",
    },
    "closed": {
        "en": "{app} is closed.",
        "hi": "{app} बंद कर दिया।",
        "hi-latn": "{app} band kar diya.",
        "ta": "{app} மூடப்பட்டது.",
        "te": "{app} మూసేశాను.",
        "kn": "{app} ಮುಚ್ಚಿದೆ.",
        "bn": "{app} বন্ধ করেছি।",
        "mr": "{app} बंद केलं.",
    },
    "volume_set": {
        "en": "Volume is now {level} percent.",
        "hi": "आवाज़ अब {level} प्रतिशत है।",
        "hi-latn": "Volume ab {level} percent hai.",
        "ta": "ஒலி அளவு இப்போது {level} சதவீதம்.",
        "te": "వాల్యూమ్ ఇప్పుడు {level} శాతం.",
        "kn": "ವಾಲ್ಯೂಮ್ ಈಗ {level} ಶೇಕಡಾ.",
        "bn": "ভলিউম এখন {level} শতাংশ।",
        "mr": "आवाज आता {level} टक्के आहे.",
    },
    "volume_muted": {
        "en": "Sound is muted.",
        "hi": "आवाज़ बंद कर दी।",
        "hi-latn": "Awaaz band kar di.",
        "ta": "ஒலி அணைக்கப்பட்டது.",
        "te": "శబ్దం ఆపేశాను.",
        "kn": "ಧ್ವನಿ ಆಫ್ ಮಾಡಿದೆ.",
        "bn": "আওয়াজ বন্ধ করেছি।",
        "mr": "आवाज बंद केला.",
    },
    "volume_unmuted": {
        "en": "Sound is on again.",
        "hi": "आवाज़ फिर से चालू कर दी।",
        "hi-latn": "Awaaz phir se chalu kar di.",
        "ta": "ஒலி மீண்டும் இயக்கப்பட்டது.",
        "te": "శబ్దం మళ్ళీ ఆన్ చేశాను.",
        "kn": "ಧ್ವನಿ ಮತ್ತೆ ಆನ್ ಮಾಡಿದೆ.",
        "bn": "আওয়াজ আবার চালু করেছি।",
        "mr": "आवाज पुन्हा चालू केला.",
    },
    "searched": {
        "en": "Here are the results for {query}.",
        "hi": "{query} के नतीजे खोल दिए।",
        "hi-latn": "{query} ke results khol diye.",
        "ta": "{query} க்கான முடிவுகள் திறக்கப்பட்டன.",
        "te": "{query} ఫలితాలు తెరిచాను.",
        "kn": "{query} ಫಲಿತಾಂಶಗಳನ್ನು ತೆರೆದಿದ್ದೇನೆ.",
        "bn": "{query} এর ফলাফল খুলেছি।",
        "mr": "{query} चे निकाल उघडले.",
    },
    "youtube_searched": {
        "en": "Showing {query} on YouTube.",
        "hi": "यूट्यूब पर {query} खोल दिया।",
        "hi-latn": "YouTube par {query} khol diya.",
        "ta": "யூடியூபில் {query} திறக்கப்பட்டது.",
        "te": "యూట్యూబ్‌లో {query} తెరిచాను.",
        "kn": "ಯೂಟ್ಯೂಬ್‌ನಲ್ಲಿ {query} ತೆರೆದಿದ್ದೇನೆ.",
        "bn": "ইউটিউবে {query} খুলেছি।",
        "mr": "यूट्यूबवर {query} उघडलं.",
    },
    "typed": {
        "en": "I have typed it.",
        "hi": "लिख दिया।",
        "hi-latn": "Likh diya.",
        "ta": "தட்டச்சு செய்துவிட்டேன்.",
        "te": "టైప్ చేశాను.",
        "kn": "ಟೈಪ್ ಮಾಡಿದೆ.",
        "bn": "লিখে দিয়েছি।",
        "mr": "लिहिलं.",
    },
    "screenshot_saved": {
        "en": "Screenshot saved.",
        "hi": "स्क्रीनशॉट सेव हो गया।",
        "hi-latn": "Screenshot save ho gaya.",
        "ta": "ஸ்கிரீன்ஷாட் சேமிக்கப்பட்டது.",
        "te": "స్క్రీన్‌షాట్ సేవ్ అయింది.",
        "kn": "ಸ್ಕ್ರೀನ್‌ಶಾಟ್ ಉಳಿಸಲಾಗಿದೆ.",
        "bn": "স্ক্রিনশট সেভ হয়েছে।",
        "mr": "स्क्रीनशॉट सेव्ह झाला.",
    },
    "status": {
        "en": "Battery is at {battery} percent, CPU {cpu} percent and memory {memory} percent.",
        "hi": "बैटरी {battery} प्रतिशत है, सीपीयू {cpu} प्रतिशत और मेमोरी {memory} प्रतिशत।",
        "hi-latn": "Battery {battery} percent hai, CPU {cpu} percent aur memory {memory} percent.",
        "ta": "பேட்டரி {battery} சதவீதம், சிபியு {cpu} சதவீதம், நினைவகம் {memory} சதவீதம்.",
        "te": "బ్యాటరీ {battery} శాతం, సీపీయూ {cpu} శాతం, మెమరీ {memory} శాతం.",
        "kn": "ಬ್ಯಾಟರಿ {battery} ಶೇಕಡಾ, ಸಿಪಿಯು {cpu} ಶೇಕಡಾ, ಮೆಮೊರಿ {memory} ಶೇಕಡಾ.",
        "bn": "ব্যাটারি {battery} শতাংশ, সিপিইউ {cpu} শতাংশ, মেমোরি {memory} শতাংশ।",
        "mr": "बॅटरी {battery} टक्के, सीपीयू {cpu} टक्के, मेमरी {memory} टक्के.",
    },
    "status_no_battery": {
        "en": "This PC has no battery. CPU is at {cpu} percent and memory {memory} percent.",
        "hi": "इस कंप्यूटर में बैटरी नहीं है। सीपीयू {cpu} प्रतिशत और मेमोरी {memory} प्रतिशत है।",
        "hi-latn": "Is computer mein battery nahi hai. CPU {cpu} percent aur memory {memory} "
        "percent hai.",
        "ta": "இந்தக் கணினியில் பேட்டரி இல்லை. சிபியு {cpu} சதவீதம், நினைவகம் {memory} சதவீதம்.",
        "te": "ఈ కంప్యూటర్‌లో బ్యాటరీ లేదు. సీపీయూ {cpu} శాతం, మెమరీ {memory} శాతం.",
        "kn": "ಈ ಕಂಪ್ಯೂಟರ್‌ನಲ್ಲಿ ಬ್ಯಾಟರಿ ಇಲ್ಲ. ಸಿಪಿಯು {cpu} ಶೇಕಡಾ, ಮೆಮೊರಿ {memory} ಶೇಕಡಾ.",
        "bn": "এই কম্পিউটারে ব্যাটারি নেই। সিপিইউ {cpu} শতাংশ, মেমোরি {memory} শতাংশ।",
        "mr": "या संगणकाला बॅटरी नाही. सीपीयू {cpu} टक्के, मेमरी {memory} टक्के.",
    },
    "time": {
        "en": "It is {time}, {date}.",
        "hi": "अभी {time} बजे हैं, आज {date} है।",
        "hi-latn": "Abhi {time} baje hain, aaj {date} hai.",
        "ta": "இப்போது நேரம் {time}, இன்று {date}.",
        "te": "ఇప్పుడు సమయం {time}, ఈ రోజు {date}.",
        "kn": "ಈಗ ಸಮಯ {time}, ಇಂದು {date}.",
        "bn": "এখন {time} বাজে, আজ {date}।",
        "mr": "आता {time} वाजले आहेत, आज {date} आहे.",
    },
    "note_saved": {
        "en": "Your note is saved.",
        "hi": "नोट सेव कर दिया।",
        "hi-latn": "Note save kar diya.",
        "ta": "குறிப்பு சேமிக்கப்பட்டது.",
        "te": "నోట్ సేవ్ చేశాను.",
        "kn": "ಟಿಪ್ಪಣಿ ಉಳಿಸಿದೆ.",
        "bn": "নোট সেভ করেছি।",
        "mr": "नोट सेव्ह केली.",
    },
    "locked": {
        "en": "The computer is locked.",
        "hi": "कंप्यूटर लॉक कर दिया।",
        "hi-latn": "Computer lock kar diya.",
        "ta": "கணினி லாக் செய்யப்பட்டது.",
        "te": "కంప్యూటర్ లాక్ చేశాను.",
        "kn": "ಕಂಪ್ಯೂಟರ್ ಲಾಕ್ ಮಾಡಿದೆ.",
        "bn": "কম্পিউটার লক করেছি।",
        "mr": "संगणक लॉक केला.",
    },
    "shutdown_scheduled": {
        "en": "The computer will shut down in {delay} seconds. Say cancel shutdown to stop it.",
        "hi": "कंप्यूटर {delay} सेकंड में बंद हो जाएगा। रोकने के लिए शटडाउन कैंसल बोलिए।",
        "hi-latn": "Computer {delay} second mein band ho jayega. Rokne ke liye shutdown cancel "
        "boliye.",
        "ta": "கணினி {delay} வினாடிகளில் அணையும். நிறுத்த ஷட்டவுன் கேன்சல் என்று சொல்லுங்கள்.",
        "te": "కంప్యూటర్ {delay} సెకన్లలో ఆగిపోతుంది. ఆపడానికి షట్‌డౌన్ క్యాన్సల్ అని చెప్పండి.",
        "kn": "ಕಂಪ್ಯೂಟರ್ {delay} ಸೆಕೆಂಡುಗಳಲ್ಲಿ ಆಫ್ ಆಗುತ್ತದೆ. ನಿಲ್ಲಿಸಲು ಶಟ್‌ಡೌನ್ ಕ್ಯಾನ್ಸಲ್ ಎಂದು ಹೇಳಿ.",
        "bn": "কম্পিউটার {delay} সেকেন্ডে বন্ধ হবে। থামাতে শাটডাউন ক্যান্সেল বলুন।",
        "mr": "संगणक {delay} सेकंदात बंद होईल. थांबवण्यासाठी शटडाउन कॅन्सल म्हणा.",
    },
    "shutdown_cancelled": {
        "en": "Shutdown cancelled.",
        "hi": "शटडाउन रोक दिया।",
        "hi-latn": "Shutdown rok diya.",
        "ta": "ஷட்டவுன் ரத்து செய்யப்பட்டது.",
        "te": "షట్‌డౌన్ రద్దు చేశాను.",
        "kn": "ಶಟ್‌ಡೌನ್ ರದ್ದು ಮಾಡಿದೆ.",
        "bn": "শাটডাউন বাতিল করেছি।",
        "mr": "शटडाउन रद्द केलं.",
    },
    "no_shutdown": {
        "en": "No shutdown was scheduled.",
        "hi": "कोई शटडाउन तय नहीं था।",
        "hi-latn": "Koi shutdown set nahi tha.",
        "ta": "எந்த ஷட்டவுனும் திட்டமிடப்படவில்லை.",
        "te": "ఏ షట్‌డౌన్ షెడ్యూల్ కాలేదు.",
        "kn": "ಯಾವುದೇ ಶಟ್‌ಡೌನ್ ನಿಗದಿಯಾಗಿರಲಿಲ್ಲ.",
        "bn": "কোনো শাটডাউন ঠিক করা ছিল না।",
        "mr": "कोणतंही शटडाउन ठरलेलं नव्हतं.",
    },
}

# "Not allowed" (the guard blocked a tool call) says the same as the older "blocked" key.
PHRASES["not_allowed"] = dict(PHRASES["blocked"])

# Single words that, on their own, mean "stop talking / stop what you are doing".
STOP_WORDS: frozenset[str] = frozenset(
    {
        # English
        "stop", "enough", "quiet", "silence", "cancel", "pause", "halt",
        # Hindi / Hinglish
        "ruko", "rukiye", "rukjao", "ruk", "bas", "chup", "thehro",
        "रुको", "रुकिए", "रुक", "बस", "चुप", "ठहरो",
        # Marathi
        "thamba", "थांब", "थांबा",
        # Tamil
        "nirutthu", "niruthu", "podhum", "நிறுத்து", "நிறுத்துங்கள்", "போதும்", "ருக",
        # Telugu
        "aapu", "aapandi", "chaalu", "ఆపు", "ఆపండి", "చాలు",
        # Kannada
        "nillisu", "saaku", "ನಿಲ್ಲಿಸು", "ನಿಲ್ಲಿಸಿ", "ಸಾಕು",
        # Bengali
        "thamo", "থামো", "থামুন", "থাম",
    }
)

# Multi-word stop commands, matched as whole phrases.
STOP_PHRASES: tuple[str, ...] = (
    "ruk jao", "ruk jaiye", "bas karo", "band karo", "chup ho jao", "chup raho",
    "stop it", "stop talking", "be quiet", "shut up", "that's enough", "thats enough",
    "रुक जाओ", "रुक जाइए", "बस करो", "बंद करो", "चुप हो जाओ", "चुप रहो",
)

# Polite or filler words allowed around a stop word ("bas ji", "please stop").
STOP_FILLERS: frozenset[str] = frozenset(
    {
        "please", "pls", "ji", "jee", "abhi", "now", "na", "yaar", "bhai", "ok", "okay",
        "जी", "अभी", "ना", "प्लीज़", "प्लीज",
    }
)

MAX_STOP_TOKENS = 4
_PUNCTUATION = ".,!?;:।॥…'\"()"


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


def is_stop_command(text: str | None) -> bool:
    """True when a short utterance is only a stop command ("ruko", "bas karo", "stop").

    Longer sentences ("Chrome band karo") are commands, not interruptions: any word
    that is not a stop word or a polite filler makes this False.
    """
    tokens = [t.strip(_PUNCTUATION) for t in (text or "").lower().split()]
    tokens = [t for t in tokens if t]
    if not tokens or len(tokens) > MAX_STOP_TOKENS:
        return False
    joined = f" {' '.join(tokens)} "
    found = False
    for stop_phrase in sorted(STOP_PHRASES, key=len, reverse=True):
        key = f" {stop_phrase} "
        while key in joined:
            joined = joined.replace(key, " ")
            found = True
    for token in joined.split():
        if token in STOP_WORDS:
            found = True
        elif token not in STOP_FILLERS:
            return False
    return found

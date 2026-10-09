"""Every visible window label in English and Hindi (PDF WS7 "Hindi and English UI labels").

Use :func:`t` with the UI language from settings (``en`` or ``hi``). The window
rebuilds its labels when the language changes in the settings dialog.

Pure data and functions only: nothing here needs a display.
"""

from __future__ import annotations

UI_LANGUAGE_NAMES: dict[str, str] = {"en": "English", "hi": "हिन्दी"}

LABELS: dict[str, dict[str, str]] = {
    # window and sections
    "app_title": {"en": "Sarvam Sahayak", "hi": "सर्वम सहायक"},
    "you_said": {"en": "You said", "hi": "आपने कहा"},
    "assistant_said": {"en": "Assistant said", "hi": "सहायक ने कहा"},
    "assistant_did": {"en": "Assistant did", "hi": "सहायक ने किया"},
    "nothing_yet": {"en": "Nothing yet", "hi": "अभी कुछ नहीं"},
    "activity": {"en": "Activity (last 20)", "hi": "गतिविधि (पिछले 20)"},
    "no_activity": {"en": "No actions yet", "hi": "अभी कोई काम नहीं हुआ"},
    "audit": {"en": "Safety log", "hi": "सुरक्षा लॉग"},
    "audit_show": {"en": "Show safety log", "hi": "सुरक्षा लॉग दिखाएँ"},
    "audit_hide": {"en": "Hide safety log", "hi": "सुरक्षा लॉग छिपाएँ"},
    "audit_refresh": {"en": "Refresh", "hi": "ताज़ा करें"},
    "no_audit": {"en": "No safety decisions yet", "hi": "अभी कोई सुरक्षा निर्णय नहीं"},
    "speed": {"en": "Speed", "hi": "गति"},
    "speed_stt": {"en": "STT", "hi": "सुनना"},
    "speed_llm": {"en": "LLM", "hi": "सोचना"},
    "speed_action": {"en": "action", "hi": "काम"},
    "speed_first_audio": {"en": "first audio", "hi": "पहली आवाज़"},
    "speed_total": {"en": "total", "hi": "कुल"},
    "ms": {"en": "ms", "hi": "मि.से."},
    "language_now": {"en": "Language", "hi": "भाषा"},
    # status bar: friendly state words
    "state_idle": {"en": "Ready", "hi": "तैयार"},
    "state_listening": {"en": "Listening…", "hi": "सुन रहा हूँ…"},
    "state_thinking": {"en": "Thinking…", "hi": "सोच रहा हूँ…"},
    "state_checking": {"en": "Checking…", "hi": "जाँच रहा हूँ…"},
    "state_awaiting_yes": {"en": "Waiting for your yes", "hi": "आपकी हाँ का इंतज़ार"},
    "state_acting": {"en": "Doing it…", "hi": "कर रहा हूँ…"},
    "state_speaking": {"en": "Speaking…", "hi": "बोल रहा हूँ…"},
    "yes_no_hint": {"en": "Say or type yes / no", "hi": "हाँ या ना बोलें या लिखें"},
    "stopped": {"en": "Stopped", "hi": "रोक दिया"},
    "problem": {"en": "Problem: {message}", "hi": "समस्या: {message}"},
    "live_failed": {"en": "Could not start live mode", "hi": "लाइव मोड शुरू नहीं हुआ"},
    # health dots (one per module)
    "mod_audio": {"en": "Mic", "hi": "माइक"},
    "mod_stt": {"en": "Hearing", "hi": "सुनना"},
    "mod_brain": {"en": "Brain", "hi": "दिमाग"},
    "mod_actions": {"en": "Actions", "hi": "काम"},
    "mod_safety": {"en": "Safety", "hi": "सुरक्षा"},
    "mod_tts": {"en": "Voice", "hi": "आवाज़"},
    # action statuses and safety verdicts
    "status_ok": {"en": "done", "hi": "हो गया"},
    "status_error": {"en": "error", "hi": "गड़बड़ी"},
    "status_denied": {"en": "denied", "hi": "मना"},
    "status_blocked": {"en": "blocked", "hi": "रोका गया"},
    "status_cancelled": {"en": "cancelled", "hi": "रद्द"},
    "status_timeout": {"en": "timed out", "hi": "समय समाप्त"},
    "verdict_allow": {"en": "allowed", "hi": "अनुमति"},
    "verdict_confirm": {"en": "asked first", "hi": "पहले पूछा"},
    "verdict_deny": {"en": "denied", "hi": "मना किया"},
    # controls
    "live": {"en": "Live mode (Ctrl+L)", "hi": "लाइव मोड (Ctrl+L)"},
    "hold_to_talk": {"en": "Hold to talk (F8)", "hi": "दबाकर बोलें (F8)"},
    "stop": {"en": "Stop (F9)", "hi": "रोकें (F9)"},
    "wake_word": {"en": "Wake word", "hi": "जगाने का शब्द"},
    "wake_placeholder": {"en": "e.g. Sarvam", "hi": "जैसे सर्वम"},
    "language_hint": {"en": "Language", "hi": "भाषा"},
    "lang_auto": {"en": "Auto", "hi": "अपने आप"},
    "mic_level": {"en": "Mic level", "hi": "माइक स्तर"},
    "type_here": {"en": "Type a command and press Enter", "hi": "आदेश लिखें और Enter दबाएँ"},
    "send": {"en": "Send", "hi": "भेजें"},
    "settings": {"en": "Settings", "hi": "सेटिंग्स"},
    # settings dialog
    "mic_device": {"en": "Microphone", "hi": "माइक्रोफ़ोन"},
    "speaker_device": {"en": "Speaker", "hi": "स्पीकर"},
    "default_device": {"en": "System default", "hi": "सिस्टम डिफ़ॉल्ट"},
    "voice": {"en": "Voice", "hi": "आवाज़"},
    "font_size": {"en": "Text size", "hi": "अक्षर का आकार"},
    "theme": {"en": "Colours", "hi": "रंग"},
    "theme_dark": {"en": "Dark", "hi": "गहरा"},
    "theme_light": {"en": "Light", "hi": "हल्का"},
    "theme_high_contrast": {"en": "High contrast", "hi": "उच्च कंट्रास्ट"},
    "ui_language": {"en": "Screen language", "hi": "स्क्रीन की भाषा"},
    "save": {"en": "Save (Enter)", "hi": "सहेजें (Enter)"},
    "cancel": {"en": "Cancel (Esc)", "hi": "रद्द करें (Esc)"},
    "saved": {"en": "Settings saved", "hi": "सेटिंग्स सहेजी गईं"},
    "save_failed": {
        "en": "Could not save settings: {error}",
        "hi": "सेटिंग्स नहीं सहेजी जा सकीं: {error}",
    },
    "restart_note": {
        "en": "Microphone, speaker and voice change after a restart.",
        "hi": "माइक्रोफ़ोन, स्पीकर और आवाज़ दोबारा शुरू करने पर बदलेंगे।",
    },
}

# Language hint dropdown: (label, BCP-47 code). "" means auto-detect.
# Hinglish is romanised Hindi, so its hint is still hi-IN.
LANGUAGE_HINTS: tuple[tuple[str, str], ...] = (
    ("lang_auto", ""),
    ("हिन्दी", "hi-IN"),
    ("English", "en-IN"),
    ("Hinglish", "hi-IN"),
    ("தமிழ்", "ta-IN"),
    ("తెలుగు", "te-IN"),
    ("বাংলা", "bn-IN"),
    ("ಕನ್ನಡ", "kn-IN"),
    ("मराठी", "mr-IN"),
    ("ગુજરાતી", "gu-IN"),
)


def ui_lang(lang: str | None) -> str:
    """Normalise a UI language: anything starting with ``hi`` is Hindi, else English."""
    return "hi" if (lang or "").strip().lower().startswith("hi") else "en"


def t(key: str, lang: str | None = "en", **values: object) -> str:
    """The label ``key`` in ``lang`` (``en`` / ``hi``), with ``{name}`` fields filled in.

    Unknown keys return the key itself so a missing label never crashes the window.
    """
    entry = LABELS.get(key)
    if entry is None:
        return key
    text = entry.get(ui_lang(lang)) or entry["en"]
    return text.format(**values) if values else text


def hint_choices(lang: str | None = "en") -> list[str]:
    """Dropdown entries for the language hint, e.g. ``Auto``, ``हिन्दी · hi-IN``."""
    return [_hint_label(label, code, lang) for label, code in LANGUAGE_HINTS]


def hint_code(choice: str, lang: str | None = "en") -> str | None:
    """The language code for a dropdown entry; ``None`` means auto-detect."""
    for label, code in LANGUAGE_HINTS:
        if _hint_label(label, code, lang) == choice:
            return code or None
    return None


def hint_choice(code: str | None, lang: str | None = "en") -> str:
    """The dropdown entry for a language code (first match; unknown codes give Auto)."""
    for label, hint in LANGUAGE_HINTS:
        if hint and hint == (code or ""):
            return _hint_label(label, hint, lang)
    return _hint_label(*LANGUAGE_HINTS[0], lang)


def _hint_label(label: str, code: str, lang: str | None) -> str:
    return f"{label} · {code}" if code else t(label, lang)

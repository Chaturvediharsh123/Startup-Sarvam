"""Application settings loaded from the environment and the project ``.env`` file.

All tunable values live in one frozen :class:`Settings` dataclass. Code never reads
``os.environ`` directly; it calls :func:`get_settings` instead.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
SETTINGS_FILE = PROJECT_ROOT / "config" / "settings.yaml"
ALLOWLIST_FILE = PROJECT_ROOT / "config" / "allowlist.yaml"

UI_THEMES = ("dark", "light", "high_contrast")
UI_LANGUAGES = ("en", "hi")

REASONING_LEVELS = ("low", "medium", "high", "max", "none")


class ConfigError(RuntimeError):
    """Raised when a setting is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    """Every setting the assistant uses. Built by :func:`load_settings`."""

    sarvam_api_key: str
    stt_realtime_model: str = "saaras:v3-realtime"
    stt_model: str = "saaras:v4"
    llm_model: str = "sarvam-105b"
    llm_reasoning: str = "low"
    tts_model: str = "bulbul:v3"
    tts_speaker: str = "priya"
    tts_sample_rate: int = 24000
    tts_pace: float = 1.0
    default_language: str = "hi-IN"
    sample_rate: int = 16000
    chunk_ms: int = 100
    vad_silence_ms: int = 500
    mic_device: str = ""
    history_turns: int = 4
    wake_word: str = ""
    confirm_timeout_s: float = 10.0
    shutdown_delay_s: int = 60
    max_actions_per_minute: int = 10
    barge_in: bool = False
    db_path: Path = PROJECT_ROOT / "data" / "assistant.db"
    log_dir: Path = PROJECT_ROOT / "logs"
    notes_dir: Path = Path.home() / "Documents" / "Sarvam Assistant"
    # -- added for the architecture template ----------------------------------
    language_hint: str = ""  # empty = auto-detect; else e.g. hi-IN
    tts_voices: Mapping[str, str] = field(default_factory=dict)  # language -> speaker
    noise_gate: float = 0.0  # 0.0-1.0 mic level below which chunks count as silence
    local_silence_ms: int = 900  # backup end-of-speech if the server VAD misses it
    stt_fallback_after: int = 3  # failed reconnects before switching to push-to-talk
    action_timeout_s: float = 5.0
    llm_timeout_s: float = 12.0
    filler_after_ms: int = 1200  # speak "ek second..." if the LLM is slower than this
    max_turn_s: float = 20.0
    stop_words_enabled: bool = True  # "ruko" / "stop" interrupts while busy
    ui_font_size: int = 20
    ui_theme: str = "dark"
    ui_language: str = "en"
    allowlist_path: Path = ALLOWLIST_FILE

    def voice_for(self, language: str | None) -> str:
        """The TTS speaker for a language, falling back to :attr:`tts_speaker`."""
        if language:
            for key in (language, language.split("-")[0]):
                if key in self.tts_voices:
                    return self.tts_voices[key]
        return self.tts_speaker

    @property
    def chunk_samples(self) -> int:
        """Number of audio samples in one mic chunk."""
        return self.sample_rate * self.chunk_ms // 1000


def _resolve_path(value: str, default: Path) -> Path:
    """Turn a config path into an absolute path. Relative paths sit under the project."""
    if not value:
        return default
    if value == ":memory:":
        return Path(value)
    path = Path(os.path.expandvars(value)).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _as_int(raw: Mapping[str, str], key: str, default: int, low: int, high: int) -> int:
    """Read an integer setting and check its range."""
    value = raw.get(key, "").strip()
    if not value:
        return default
    try:
        number = int(value)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a whole number, got {value!r}") from exc
    if not low <= number <= high:
        raise ConfigError(f"{key} must be between {low} and {high}, got {number}")
    return number


def _as_float(raw: Mapping[str, str], key: str, default: float, low: float, high: float) -> float:
    """Read a decimal setting and check its range."""
    value = raw.get(key, "").strip()
    if not value:
        return default
    try:
        number = float(value)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {value!r}") from exc
    if not low <= number <= high:
        raise ConfigError(f"{key} must be between {low} and {high}, got {number}")
    return number


def _as_bool(raw: Mapping[str, str], key: str, default: bool) -> bool:
    """Read a true/false setting."""
    value = raw.get(key, "").strip().lower()
    if not value:
        return default
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{key} must be true or false, got {value!r}")


def _read_yaml(settings_file: Path | None) -> dict[str, str]:
    """Flatten ``config/settings.yaml`` into ``ENV_NAME -> text``.

    Sections (``sarvam``, ``audio``, ``stt``, ``brain``, ``tts``, ``safety``, ``ui``) only
    group keys; each key is the lower-case form of its ``.env`` name, e.g.
    ``brain: {history_turns: 4}`` is ``HISTORY_TURNS=4``. ``tts_voices`` is a mapping.
    """
    if settings_file is None or not settings_file.is_file():
        return {}
    import yaml

    data: Any = yaml.safe_load(settings_file.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{settings_file.name} must contain sections of key: value pairs")
    flat: dict[str, str] = {}
    for section, values in data.items():
        if not isinstance(values, dict):
            raise ConfigError(f"{settings_file.name}: section {section!r} must be a mapping")
        for key, value in values.items():
            if str(key).lower() == "sarvam_api_key":
                raise ConfigError("Put SARVAM_API_KEY in .env, never in settings.yaml")
            if isinstance(value, dict):
                value = ",".join(f"{k}:{v}" for k, v in value.items())
            elif isinstance(value, bool):
                value = "true" if value else "false"
            flat[str(key).upper()] = "" if value is None else str(value)
    return flat


def _as_mapping(raw: Mapping[str, str], key: str) -> dict[str, str]:
    """Read ``a:b,c:d`` into a dict (used for per-language voices)."""
    value = raw.get(key, "").strip()
    result: dict[str, str] = {}
    for part in filter(None, (p.strip() for p in value.split(","))):
        lang, sep, speaker = part.partition(":")
        if not sep or not lang.strip() or not speaker.strip():
            raise ConfigError(f"{key} must look like hi-IN:priya,ta-IN:kavya, got {part!r}")
        result[lang.strip()] = speaker.strip().lower()
    return result


def _as_choice(raw: Mapping[str, str], key: str, default: str, choices: tuple[str, ...]) -> str:
    """Read a setting that must be one of ``choices``."""
    value = raw.get(key, "").strip().lower() or default
    if value not in choices:
        raise ConfigError(f"{key} must be one of {choices}, got {value!r}")
    return value


def _read_raw(env_file: Path | None, settings_file: Path | None = None) -> dict[str, str]:
    """Merge settings.yaml, the .env file and real environment variables.

    Later sources win: ``settings.yaml`` < ``.env`` < environment.
    """
    raw: dict[str, str] = _read_yaml(settings_file)
    if env_file is not None and env_file.is_file():
        raw.update({k: v for k, v in dotenv_values(env_file).items() if v is not None})
    raw.update(os.environ)
    return raw


MOCK_API_KEY = "mock-mode-no-key"


def _api_key(raw: Mapping[str, str], require_key: bool) -> str:
    """The Sarvam key, or :data:`MOCK_API_KEY` when no key is required."""
    key = raw.get("SARVAM_API_KEY", "").strip()
    if key and key != "sk_your_key_here":
        return key
    if not require_key:
        return MOCK_API_KEY
    raise ConfigError(
        "SARVAM_API_KEY is not set. Copy .env.example to .env in the project folder "
        "and put your key from https://dashboard.sarvam.ai in it."
    )


def load_settings(
    env_file: Path | None = ENV_FILE,
    overrides: Mapping[str, str] | None = None,
    require_key: bool = True,
    settings_file: Path | None = SETTINGS_FILE,
) -> Settings:
    """Build :class:`Settings` from the .env file, the environment and ``overrides``.

    Args:
        require_key: False for ``--mock`` mode, which never calls Sarvam; a missing
            key is then replaced by :data:`MOCK_API_KEY`.

    Raises:
        ConfigError: if ``SARVAM_API_KEY`` is missing (and required) or a value is invalid.
    """
    raw = _read_raw(env_file, settings_file)
    if overrides:
        raw.update(overrides)
    key = _api_key(raw, require_key)
    reasoning = raw.get("SARVAM_LLM_REASONING", "low").strip().lower() or "low"
    if reasoning not in REASONING_LEVELS:
        raise ConfigError(f"SARVAM_LLM_REASONING must be one of {REASONING_LEVELS}")
    sample_rate = _as_int(raw, "SAMPLE_RATE", 16000, 8000, 16000)
    if sample_rate not in (8000, 16000):
        raise ConfigError("SAMPLE_RATE must be 8000 or 16000 (Sarvam realtime STT limit)")
    defaults = Settings(sarvam_api_key=key)
    return Settings(
        sarvam_api_key=key,
        stt_realtime_model=raw.get("SARVAM_STT_REALTIME_MODEL", "").strip()
        or defaults.stt_realtime_model,
        stt_model=raw.get("SARVAM_STT_MODEL", "").strip() or defaults.stt_model,
        llm_model=raw.get("SARVAM_LLM_MODEL", "").strip() or defaults.llm_model,
        llm_reasoning=reasoning,
        tts_model=raw.get("SARVAM_TTS_MODEL", "").strip() or defaults.tts_model,
        tts_speaker=raw.get("TTS_SPEAKER", "").strip().lower() or defaults.tts_speaker,
        tts_sample_rate=_as_int(raw, "TTS_SAMPLE_RATE", 24000, 8000, 24000),
        tts_pace=_as_float(raw, "TTS_PACE", 1.0, 0.5, 2.0),
        default_language=raw.get("DEFAULT_LANGUAGE", "").strip() or defaults.default_language,
        sample_rate=sample_rate,
        chunk_ms=_as_int(raw, "CHUNK_MS", 100, 20, 500),
        vad_silence_ms=_as_int(raw, "VAD_SILENCE_MS", 500, 100, 5000),
        mic_device=raw.get("MIC_DEVICE", "").strip(),
        history_turns=_as_int(raw, "HISTORY_TURNS", 4, 0, 100),
        wake_word=raw.get("WAKE_WORD", "").strip(),
        confirm_timeout_s=_as_float(raw, "CONFIRM_TIMEOUT_S", 10.0, 1.0, 60.0),
        shutdown_delay_s=_as_int(raw, "SHUTDOWN_DELAY_S", 60, 10, 3600),
        max_actions_per_minute=_as_int(raw, "MAX_ACTIONS_PER_MINUTE", 10, 1, 120),
        barge_in=_as_bool(raw, "BARGE_IN", False),
        db_path=_resolve_path(raw.get("DB_PATH", "").strip(), defaults.db_path),
        log_dir=_resolve_path(raw.get("LOG_DIR", "").strip(), defaults.log_dir),
        notes_dir=_resolve_path(raw.get("NOTES_DIR", "").strip(), defaults.notes_dir),
        language_hint=raw.get("LANGUAGE_HINT", "").strip(),
        tts_voices=_as_mapping(raw, "TTS_VOICES"),
        noise_gate=_as_float(raw, "NOISE_GATE", 0.0, 0.0, 1.0),
        local_silence_ms=_as_int(raw, "LOCAL_SILENCE_MS", 900, 300, 5000),
        stt_fallback_after=_as_int(raw, "STT_FALLBACK_AFTER", 3, 1, 20),
        action_timeout_s=_as_float(raw, "ACTION_TIMEOUT_S", 5.0, 0.5, 60.0),
        llm_timeout_s=_as_float(raw, "LLM_TIMEOUT_S", 12.0, 1.0, 120.0),
        filler_after_ms=_as_int(raw, "FILLER_AFTER_MS", 1200, 0, 10000),
        max_turn_s=_as_float(raw, "MAX_TURN_S", 20.0, 5.0, 300.0),
        stop_words_enabled=_as_bool(raw, "STOP_WORDS_ENABLED", True),
        ui_font_size=_as_int(raw, "UI_FONT_SIZE", 20, 14, 40),
        ui_theme=_as_choice(raw, "UI_THEME", "dark", UI_THEMES),
        ui_language=_as_choice(raw, "UI_LANGUAGE", "en", UI_LANGUAGES),
        allowlist_path=_resolve_path(raw.get("ALLOWLIST_PATH", "").strip(), ALLOWLIST_FILE),
    )


def save_env_values(values: Mapping[str, str], env_file: Path = ENV_FILE) -> None:
    """Update (or add) ``KEY=value`` lines in the .env file, keeping every other line."""
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.is_file() else []
    pending = dict(values)
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in pending and not line.lstrip().startswith("#"):
            lines[index] = f"{key}={pending.pop(key)}"
    lines.extend(f"{key}={value}" for key, value in pending.items())
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, loading them once."""
    return load_settings()

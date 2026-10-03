"""Action Block configuration (environment variables prefixed ``ACTIONS_`` or a ``.env`` file)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEntry(BaseModel):
    """An application that ``open_app`` / ``close_app`` may touch.

    ``command`` is an argv list started without a shell, so no shell syntax is ever interpreted.
    """

    command: list[str] = Field(min_length=1)
    window_title_re: str


DEFAULT_APPS: dict[str, AppEntry] = {
    "notepad": AppEntry(command=["notepad.exe"], window_title_re=".*Notepad.*"),
    "calculator": AppEntry(command=["calc.exe"], window_title_re=".*Calculator.*"),
    "paint": AppEntry(command=["mspaint.exe"], window_title_re=".*Paint.*"),
}

DEFAULT_ALLOWED_HOTKEYS: list[str] = [
    "ctrl+a", "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+z", "ctrl+y",
    "ctrl+s", "ctrl+f", "ctrl+n", "ctrl+t", "ctrl+w", "ctrl+l",
    "ctrl+plus", "ctrl+minus", "ctrl+0",
    "alt+tab", "alt+left", "alt+right",
]

DEFAULT_ALLOWED_KEYS: list[str] = [
    "enter", "tab", "escape", "backspace", "delete", "space",
    "up", "down", "left", "right", "home", "end", "pageup", "pagedown",
    *[f"f{i}" for i in range(1, 13)],
]


class ActionSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ACTIONS_", env_file=".env", extra="ignore")

    default_timeout_s: float = Field(default=30.0, gt=0)
    max_timeout_s: float = Field(default=120.0, gt=0)
    max_attempts_cap: int = Field(default=3, ge=1, le=5)

    evidence_dir: Path = Path(".data/evidence")
    download_dir: Path = Path(".data/downloads")
    screenshot_on_failure: bool = True
    visual_evidence: bool = True  # before/after screenshots around coordinate-based actions

    browser_headless: bool = False
    browser_channel: str = "chromium"
    browser_user_data_dir: Path | None = Path(".data/browser-profile")

    # OCR for screen targets given as text: auto = Tesseract if installed, else Windows OCR.
    ocr_engine: Literal["auto", "windows", "tesseract"] = "auto"
    ocr_languages: list[str] = Field(default_factory=lambda: ["en", "hi"])
    tesseract_cmd: str | None = None

    apps: dict[str, AppEntry] = Field(default_factory=lambda: dict(DEFAULT_APPS))
    allowed_hotkeys: list[str] = Field(default_factory=lambda: list(DEFAULT_ALLOWED_HOTKEYS))
    allowed_keys: list[str] = Field(default_factory=lambda: list(DEFAULT_ALLOWED_KEYS))
    disabled_capabilities: list[str] = Field(default_factory=list)

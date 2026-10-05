"""Push-to-talk / fallback speech-to-text (``POST /speech-to-text``).

Sends a short WAV recording with ``saaras:v4`` and ``language_code=unknown`` for
automatic language detection. The API accepts at most 30 seconds per request.
See docs/sarvam_api_notes.md, section 2.
"""

from __future__ import annotations

import io
import logging
import wave

from assistant.config import Settings
from sarvam.client import SarvamClient

logger = logging.getLogger(__name__)

STT_PATH = "/speech-to-text"
MAX_SECONDS = 29.5


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap 16-bit mono PCM in a WAV container."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


class RestSTT:
    """Transcribes one recording and detects its language."""

    def __init__(self, client: SarvamClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    def transcribe(self, pcm: bytes, sample_rate: int) -> tuple[str, str | None]:
        """Return ``(transcript, language_code)`` for 16-bit mono PCM audio.

        Audio longer than the API limit is trimmed to its last ~30 seconds.
        """
        max_bytes = int(MAX_SECONDS * sample_rate) * 2
        if len(pcm) > max_bytes:
            logger.warning("Recording longer than %.0fs; keeping the end", MAX_SECONDS)
            pcm = pcm[-max_bytes:]
        if len(pcm) < sample_rate // 5 * 2:  # under 0.2 s: nothing useful said
            return "", None
        result = self.client.post_multipart(
            STT_PATH,
            data={"model": self.settings.stt_model, "language_code": "unknown"},
            files={"file": ("speech.wav", pcm_to_wav(pcm, sample_rate), "audio/wav")},
        )
        text = str(result.get("transcript") or "").strip()
        return text, result.get("language_code")

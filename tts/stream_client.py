"""Bulbul text-to-speech with streaming playback in mind.

Uses ``POST /text-to-speech/stream`` with ``output_audio_codec=linear16`` so audio
chunks are raw 16-bit PCM that can be played as they arrive. Long replies are
split into sentences, one request each, so the first sentence starts playing
quickly. If streaming fails before any audio arrives, the REST endpoint
``POST /text-to-speech`` is used instead. See docs/sarvam_api_notes.md, sections 4–5.
"""

from __future__ import annotations

import base64
import io
import logging
import re
import struct
import wave
from collections.abc import Iterator
from typing import Any

import httpx

from core.config import Settings
from language.detect import to_tts_language
from sarvam.client import SarvamClient, SarvamError

logger = logging.getLogger(__name__)

STREAM_PATH = "/text-to-speech/stream"
REST_PATH = "/text-to-speech"
MAX_SENTENCE_CHARS = 450
MIN_SENTENCE_CHARS = 25
_SENTENCE_END = re.compile(r"(?<=[.!?।॥])\s+|\n+")


def split_sentences(text: str, max_chars: int = MAX_SENTENCE_CHARS) -> list[str]:
    """Split ``text`` into speakable pieces.

    Very short pieces are merged with the next one (fewer requests); pieces longer
    than ``max_chars`` are cut at the last space.
    """
    raw = [p.strip() for p in _SENTENCE_END.split(text) if p and p.strip()]
    merged: list[str] = []
    for piece in raw:
        if merged and len(merged[-1]) < MIN_SENTENCE_CHARS:
            merged[-1] = f"{merged[-1]} {piece}"
        else:
            merged.append(piece)
    out: list[str] = []
    for piece in merged:
        while len(piece) > max_chars:
            cut = piece.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            out.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if piece:
            out.append(piece)
    return out


def wav_data_offset(buffer: bytes) -> int | None:
    """Return where PCM data starts in a WAV header, or ``None`` if more bytes are needed.

    Returns 0 when ``buffer`` is not a WAV file (already raw PCM).
    """
    if len(buffer) < 12:
        return None if b"RIFF".startswith(buffer[:4]) else 0
    if buffer[:4] != b"RIFF" or buffer[8:12] != b"WAVE":
        return 0
    pos = 12
    while pos + 8 <= len(buffer):
        chunk_id = buffer[pos : pos + 4]
        (size,) = struct.unpack("<I", buffer[pos + 4 : pos + 8])
        if chunk_id == b"data":
            return pos + 8
        pos += 8 + size + (size & 1)
    return None


def _pcm_chunks(byte_iter: Iterator[bytes]) -> Iterator[bytes]:
    """Strip an optional WAV header and keep chunks aligned to 2-byte samples."""
    head = b""
    started = False
    carry = b""
    for chunk in byte_iter:
        if not chunk:
            continue
        if not started:
            head += chunk
            offset = wav_data_offset(head)
            if offset is None:
                continue
            chunk, started = head[offset:], True
        data = carry + chunk
        cut = len(data) - (len(data) % 2)
        carry = data[cut:]
        if cut:
            yield data[:cut]
    if not started and head:
        offset = wav_data_offset(head) or 0
        if len(head) - offset >= 2:
            yield head[offset : len(head) - ((len(head) - offset) % 2)]


def wav_to_pcm(wav_bytes: bytes) -> tuple[bytes, int]:
    """Decode a WAV file to (16-bit mono PCM, sample rate)."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        if wav.getsampwidth() != 2 or wav.getnchannels() != 1:
            raise SarvamError("Unexpected TTS WAV format (need 16-bit mono)")
        return wav.readframes(wav.getnframes()), wav.getframerate()


class SarvamTTS:
    """Turns reply text into a stream of PCM chunks (16-bit mono, ``sample_rate`` Hz)."""

    def __init__(self, client: SarvamClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self.sample_rate = settings.tts_sample_rate

    def payload(self, text: str, language: str | None) -> dict[str, Any]:
        """Request body shared by the streaming and REST endpoints."""
        return {
            "text": text,
            "language_code": to_tts_language(language),
            "speaker": self.settings.tts_speaker,
            "model": self.settings.tts_model,
            "pace": self.settings.tts_pace,
            "speech_sample_rate": self.sample_rate,
        }

    def stream(self, text: str, language: str | None) -> Iterator[bytes]:
        """Yield PCM chunks for ``text``, sentence by sentence."""
        for sentence in split_sentences(text):
            yield from self._speak_sentence(sentence, language)

    def _speak_sentence(self, sentence: str, language: str | None) -> Iterator[bytes]:
        produced = False
        try:
            body = {**self.payload(sentence, language), "output_audio_codec": "linear16"}
            with self.client.stream_post(STREAM_PATH, body) as response:
                for pcm in _pcm_chunks(response.iter_bytes()):
                    produced = True
                    yield pcm
            return
        except (SarvamError, httpx.HTTPError) as exc:
            if produced:
                logger.error("TTS stream broke mid-sentence: %s", exc)
                return
            logger.warning("TTS streaming failed (%s); using REST fallback", exc)
        yield from self._rest_sentence(sentence, language)

    def _rest_sentence(self, sentence: str, language: str | None) -> Iterator[bytes]:
        body = {**self.payload(sentence, language), "output_audio_codec": "wav"}
        result = self.client.post_json(REST_PATH, body)
        audios = result.get("audios") or []
        if not audios:
            raise SarvamError("TTS returned no audio")
        pcm, rate = wav_to_pcm(base64.b64decode(audios[0]))
        if rate != self.sample_rate:
            logger.warning("TTS fallback returned %d Hz, expected %d Hz", rate, self.sample_rate)
        step = self.sample_rate // 10 * 2  # ~100 ms per chunk
        for start in range(0, len(pcm), step):
            yield pcm[start : start + step]

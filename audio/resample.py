"""Sample-rate conversion for 16-bit mono PCM (device rate -> 16 kHz for STT).

Many Windows microphones only open at 44.1 or 48 kHz. Sarvam realtime STT accepts
8 or 16 kHz, so :class:`audio.mic.Microphone` captures at the device rate and
converts each 100 ms chunk with :func:`resample_int16`.
"""

from __future__ import annotations

from math import gcd

import numpy as np


def resample_int16(pcm: bytes, from_rate: int, to_rate: int) -> bytes:
    """Convert 16-bit mono PCM from ``from_rate`` to ``to_rate`` Hz.

    Uses a polyphase filter (``scipy.signal.resample_poly``), which is fast and has
    a proper anti-aliasing low-pass. A 100 ms chunk at 44.1 or 48 kHz becomes
    exactly 1600 samples at 16 kHz.

    Args:
        pcm: little-endian int16 samples; a trailing odd byte is ignored.
        from_rate: sample rate of ``pcm``.
        to_rate: wanted sample rate.

    Raises:
        ValueError: if a rate is not positive.
    """
    if from_rate <= 0 or to_rate <= 0:
        raise ValueError(f"sample rates must be positive, got {from_rate} -> {to_rate}")
    pcm = pcm[: len(pcm) - len(pcm) % 2]
    if from_rate == to_rate or not pcm:
        return pcm
    from scipy.signal import resample_poly

    divisor = gcd(from_rate, to_rate)
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    out = resample_poly(samples, to_rate // divisor, from_rate // divisor)
    return np.clip(np.rint(out), -32768, 32767).astype(np.int16).tobytes()


def to_mono_int16(pcm: bytes, channels: int) -> bytes:
    """Average interleaved int16 channels down to mono (used for stereo WAV samples)."""
    if channels <= 1:
        return pcm[: len(pcm) - len(pcm) % 2]
    frame = 2 * channels
    samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % frame], dtype=np.int16)
    mono = samples.reshape(-1, channels).astype(np.int32).mean(axis=1)
    return np.rint(mono).astype(np.int16).tobytes()

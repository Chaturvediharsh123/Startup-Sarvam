"""Generate the small synthetic WAV files used by ``audio.fake.FakeMicrophone`` and tests.

Real recorded speech is not available in the repository, so these are synthetic:
16 kHz, 16-bit, mono, deterministic (fixed random seed). Run from the project root:

    python tests/audio_samples/make_samples.py
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

RATE = 16000
HERE = Path(__file__).resolve().parent


def _seconds(seconds: float) -> int:
    return int(RATE * seconds)


def tone_then_silence() -> np.ndarray:
    """0.6 s of a 440 Hz tone (with 20 ms fades), then 1.0 s of digital silence."""
    t = np.arange(_seconds(0.6)) / RATE
    tone = 0.3 * np.sin(2 * np.pi * 440 * t)
    fade = _seconds(0.02)
    ramp = np.linspace(0.0, 1.0, fade)
    tone[:fade] *= ramp
    tone[-fade:] *= ramp[::-1]
    return np.concatenate([tone, np.zeros(_seconds(1.0))])


def speech_like() -> np.ndarray:
    """1.2 s of syllable-like bursts (harmonics + noise, 4 Hz envelope), then 1.0 s quiet."""
    rng = np.random.default_rng(7)
    t = np.arange(_seconds(1.2)) / RATE
    pitch = 140 + 20 * np.sin(2 * np.pi * 0.8 * t)
    phase = 2 * np.pi * np.cumsum(pitch) / RATE
    voiced = sum(np.sin(k * phase) / k for k in range(1, 6))
    envelope = np.clip(np.sin(2 * np.pi * 4 * t), 0.0, None) ** 0.5
    burst = 0.25 * envelope * (voiced / 2.3 + 0.15 * rng.standard_normal(t.size))
    floor = 0.002 * rng.standard_normal(_seconds(1.0))
    return np.concatenate([burst, floor])


def room_noise() -> np.ndarray:
    """1.0 s of a quiet room noise floor (below a typical noise gate)."""
    rng = np.random.default_rng(11)
    return 0.003 * rng.standard_normal(_seconds(1.0))


SAMPLES = {
    "01_tone_then_silence.wav": tone_then_silence,
    "02_speech_like.wav": speech_like,
    "03_room_noise.wav": room_noise,
}


def write_wav(path: Path, samples: np.ndarray) -> None:
    """Write float samples in -1..1 as 16-bit mono PCM."""
    pcm = np.clip(np.rint(samples * 32767), -32768, 32767).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(pcm)


def main() -> None:
    """Write every sample next to this script."""
    for name, make in SAMPLES.items():
        write_wav(HERE / name, make())
        print(f"wrote {name}")


if __name__ == "__main__":
    main()

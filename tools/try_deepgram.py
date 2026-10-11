"""Live check of the Deepgram push-to-talk client (uses DEEPGRAM_API_KEY from .env).

Records from the default microphone, or reads a 16-bit mono WAV file, sends it to
Deepgram once and prints the transcript, the detected language and the latency.

Usage::

    python tools/try_deepgram.py                  # record 5 seconds from the mic
    python tools/try_deepgram.py --seconds 3
    python tools/try_deepgram.py --wav clip.wav   # transcribe a recording instead
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SAMPLE_RATE = 16000


def record(seconds: float) -> bytes:
    """Record 16-bit mono PCM from the default microphone."""
    import sounddevice as sd

    print(f"Recording {seconds:g} s... speak now.")
    audio = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype="int16")
    sd.wait()
    print("Done recording.")
    return audio.tobytes()


def read_wav(path: Path) -> tuple[bytes, int]:
    """Read a 16-bit mono WAV file; returns ``(pcm, sample_rate)``."""
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2 or wav.getnchannels() != 1:
            raise SystemExit(f"{path}: need 16-bit mono WAV")
        return wav.readframes(wav.getnframes()), wav.getframerate()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=float, default=5.0, help="mic recording length")
    parser.add_argument("--wav", type=Path, help="transcribe this WAV file instead of the mic")
    args = parser.parse_args()

    from dotenv import load_dotenv

    from stt.deepgram_batch import DeepgramBatchSTT, DeepgramError

    load_dotenv(ROOT / ".env")
    key = os.environ.get("DEEPGRAM_API_KEY", "")
    if not key.strip():
        print("DEEPGRAM_API_KEY is not set. Add it to .env in the project folder.")
        return 2

    pcm, rate = read_wav(args.wav) if args.wav else (record(args.seconds), SAMPLE_RATE)
    stt = DeepgramBatchSTT(key)
    start = time.perf_counter()
    try:
        text, language = stt.transcribe(pcm, rate)
    except DeepgramError as exc:
        print(f"Deepgram error: {exc}")
        return 1
    finally:
        stt.close()
    print(f"Transcript: {text or '(nothing heard)'}")
    print(f"Language:   {language}")
    print(f"Latency:    {(time.perf_counter() - start) * 1000:.0f} ms")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to a legacy code page
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())

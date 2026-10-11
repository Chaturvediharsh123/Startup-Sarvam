"""Live check of the Cartesia TTS client (uses CARTESIA_API_KEY from .env).

First list voices for a language and pick one, then speak a sentence through the
default speaker. Prints the time to the first audio chunk and the total time.

Usage::

    python tools/try_cartesia.py --list-voices                 # Hindi voices
    python tools/try_cartesia.py --list-voices --language en
    python tools/try_cartesia.py --voice <id> --text "Chrome khol raha hoon."
    python tools/try_cartesia.py --voice <id> --text "Opening Chrome." --language en-IN

``--voice`` may be left out when CARTESIA_VOICE_ID is set in .env.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def list_voices(key: str, language: str) -> int:
    """Print id and name of up to 50 voices for a Cartesia language code."""
    import httpx

    from tts.cartesia_client import API_VERSION, BASE_URL

    response = httpx.get(
        f"{BASE_URL}/voices",
        params={"language": language, "limit": 50},
        headers={"Authorization": f"Bearer {key}", "Cartesia-Version": API_VERSION},
        timeout=15,
    )
    if response.status_code != 200:
        print(f"Cartesia error: HTTP {response.status_code}: {response.text[:200]}")
        return 1
    voices = response.json().get("data") or []
    if not voices:
        print(f"No voices found for language {language!r}.")
        return 1
    for voice in voices:
        print(f"{voice.get('id')}  {voice.get('name')}")
    return 0


def speak(key: str, voice: str, text: str, language: str) -> int:
    """Stream ``text`` to the default speaker and report timings."""
    import sounddevice as sd

    from tts.cartesia_client import CartesiaError, CartesiaTTS

    tts = CartesiaTTS(key, voice)
    start = time.perf_counter()
    first_ms: float | None = None
    total_bytes = 0
    try:
        with sd.RawOutputStream(samplerate=tts.sample_rate, channels=1, dtype="int16") as out:
            for chunk in tts.stream(text, language):
                if first_ms is None:
                    first_ms = (time.perf_counter() - start) * 1000
                total_bytes += len(chunk)
                out.write(chunk)
    except CartesiaError as exc:
        print(f"Cartesia error: {exc}")
        return 1
    finally:
        tts.close()
    seconds = total_bytes / 2 / tts.sample_rate
    print(f"First audio: {first_ms:.0f} ms" if first_ms is not None else "No audio returned.")
    print(f"Audio length: {seconds:.1f} s, total time: {time.perf_counter() - start:.1f} s")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list-voices", action="store_true", help="list voices and exit")
    parser.add_argument("--voice", help="voice id (default: CARTESIA_VOICE_ID from .env)")
    parser.add_argument("--text", default="Chrome khol raha hoon.", help="what to say")
    parser.add_argument("--language", default="hi-IN",
                        help="our code (hi-IN, en-IN); with --list-voices a Cartesia code (hi, en)")
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    key = os.environ.get("CARTESIA_API_KEY", "").strip()
    if not key:
        print("CARTESIA_API_KEY is not set. Add it to .env in the project folder.")
        return 2
    if args.list_voices:
        return list_voices(key, args.language.split("-")[0])
    voice = (args.voice or os.environ.get("CARTESIA_VOICE_ID", "")).strip()
    if not voice:
        print("No voice id. Run with --list-voices, then pass --voice <id> "
              "or set CARTESIA_VOICE_ID in .env.")
        return 2
    return speak(key, voice, args.text, args.language)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to a legacy code page
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())

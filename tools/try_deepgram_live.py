"""Live check of the Deepgram streaming client (uses DEEPGRAM_API_KEY from .env).

Streams the default microphone to Deepgram, shows live text as you speak and prints
each final sentence with the time from when the live text stopped changing (about
when you stopped speaking) to the final. ``(local backup)`` means the client's own
silence timeout ended the sentence instead of Deepgram. Press Ctrl+C to stop.

Usage::

    python tools/try_deepgram_live.py
    python tools/try_deepgram_live.py --endpointing 300     # shorter pause ends a sentence
    python tools/try_deepgram_live.py --language multi      # code-switching mode instead of hi
    python tools/try_deepgram_live.py --seconds 60          # stop after 60 s
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--endpointing", type=int, help="pause in ms that ends a sentence")
    parser.add_argument("--language", default="hi",
                        help="Deepgram language: hi (default), multi, en, ...")
    parser.add_argument("--seconds", type=float, default=120.0, help="stop after this long")
    args = parser.parse_args()

    import sounddevice as sd
    from dotenv import load_dotenv

    from core.config import load_settings
    from stt.deepgram_realtime import DeepgramRealtimeSTT

    load_dotenv(ROOT / ".env")
    key = os.environ.get("DEEPGRAM_API_KEY", "").strip()
    if not key:
        print("DEEPGRAM_API_KEY is not set. Add it to .env in the project folder.")
        return 2
    settings = load_settings(require_key=False)

    chunks: queue.Queue[bytes] = queue.Queue()
    marks = {"text_changed": 0.0, "local": 0.0}
    last = {"partial": ""}

    def on_audio(indata: bytes, frames: int, when: object, status: object) -> None:
        chunks.put(bytes(indata))

    def read_audio(timeout: float) -> bytes | None:
        try:
            return chunks.get(timeout=timeout)
        except queue.Empty:
            return None

    def on_partial(text: str) -> None:
        if text != last["partial"]:
            last["partial"] = text
            marks["text_changed"] = time.perf_counter()
        print(f"\r  ... {text[-90:]:<90}", end="", flush=True)

    def on_final(text: str, language: str | None) -> None:
        print(f"\r{'':<96}\rFINAL [{language}] {text}")
        if marks["text_changed"]:
            since = (time.perf_counter() - marks["text_changed"]) * 1000
            how = " (local backup)" if marks["local"] else ""
            print(f"      {since:.0f} ms after the live text stopped changing{how}")
        marks["text_changed"] = marks["local"] = 0.0
        last["partial"] = ""

    def on_event(event: str) -> None:
        if event == "local_end_of_speech":
            marks["local"] = time.perf_counter()
        elif event in ("connected", "disconnected", "fallback") or event.startswith("error:"):
            print(f"\r[{event}]")

    stt = DeepgramRealtimeSTT(key, settings, on_partial, on_final, on_event,
                              language=args.language, endpointing_ms=args.endpointing)
    stop = threading.Event()
    worker = threading.Thread(target=stt.run_blocking, args=(read_audio, stop), daemon=True)
    block = settings.sample_rate // 10  # 100 ms chunks
    print("Listening... speak, pause, speak again. Ctrl+C to stop.")
    try:
        with sd.RawInputStream(samplerate=settings.sample_rate, channels=1, dtype="int16",
                               blocksize=block, callback=on_audio):
            worker.start()
            deadline = time.monotonic() + args.seconds
            while worker.is_alive() and time.monotonic() < deadline:
                time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        worker.join(3)
    print("\nStopped.")
    return 0 if not stt.last_error.startswith("API key") else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to a legacy code page
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())

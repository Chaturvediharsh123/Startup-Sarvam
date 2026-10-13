"""A/B test Deepgram keyterms on the same recordings (uses DEEPGRAM_API_KEY from .env).

Live tests cannot compare settings fairly, because nobody says a sentence the same
way twice. This tool records each test phrase once, then streams the same audio
through :class:`stt.deepgram_realtime.DeepgramRealtimeSTT` without and with
keyterms and prints both transcripts side by side.

Recordings are saved as WAV files in ``data/clips/`` (git-ignored: they are your voice).

Usage::

    python tools/stt_ab.py record                    # record every phrase (4 s each)
    python tools/stt_ab.py record --only chrome      # re-record phrases matching "chrome"
    python tools/stt_ab.py run                       # compare without / with keyterms
    python tools/stt_ab.py run --batch               # also compare push-to-talk (multi)
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CLIPS = ROOT / "data" / "clips"
SAMPLE_RATE = 16000
RECORD_S = 4.0
TAIL_SILENCE_S = 1.5  # lets Deepgram's end of speech fire after the last word

# (file name, what to say, words that must appear in a correct transcript)
PHRASES: list[tuple[str, str, tuple[str, ...]]] = [
    ("notepad_kholo", "Notepad kholo", ("notepad",)),
    ("chrome_kholo", "Chrome kholo", ("chrome",)),
    ("chrome_band", "Chrome band kar do", ("chrome",)),
    ("youtube_arijit", "YouTube pe Arijit Singh ke gaane chalao", ("youtube", "arijit")),
    ("calculator", "Calculator kholo", ("calculator",)),
    ("powerpoint", "PowerPoint open karo", ("powerpoint",)),
    ("excel", "Excel kholo", ("excel",)),
    ("file_explorer", "Open File Explorer", ("file explorer",)),
]


def record(only: str | None) -> int:
    """Record each phrase (or those matching ``only``) into ``data/clips``."""
    import sounddevice as sd

    CLIPS.mkdir(parents=True, exist_ok=True)
    for name, text, _ in PHRASES:
        if only and only not in name:
            continue
        input(f'\nNext: "{text}"  -> press Enter, then say it (you have {RECORD_S:g} s)')
        audio = sd.rec(int(RECORD_S * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1,
                       dtype="int16")
        sd.wait()
        with wave.open(str(CLIPS / f"{name}.wav"), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(SAMPLE_RATE)
            out.writeframes(audio.tobytes())
        print(f"saved {name}.wav")
    return 0


def read_clip(name: str) -> bytes | None:
    path = CLIPS / f"{name}.wav"
    if not path.is_file():
        return None
    with wave.open(str(path), "rb") as clip:
        return clip.readframes(clip.getnframes())


def stream(key: str, settings: object, pcm: bytes, keyterms: list[str]) -> str:
    """Send one clip through the streaming client as fast as possible; return the text."""
    from stt.deepgram_realtime import DeepgramRealtimeSTT

    step = SAMPLE_RATE // 10 * 2  # 100 ms
    audio = pcm + b"\x00\x00" * int(SAMPLE_RATE * TAIL_SILENCE_S)
    chunks = [audio[i : i + step] for i in range(0, len(audio), step)]
    finals: list[str] = []
    stop = threading.Event()
    sent_all = [0.0]

    def read_audio(timeout: float) -> bytes | None:
        if chunks:
            return chunks.pop(0)
        if not sent_all[0]:
            sent_all[0] = time.monotonic()
        waited = time.monotonic() - sent_all[0]
        if (finals and waited > 0.5) or waited > 4.0:
            stop.set()
        time.sleep(0.05)
        return None

    client = DeepgramRealtimeSTT(key, settings, lambda t: None,  # type: ignore[arg-type]
                                 lambda text, lang: finals.append(text), language="hi",
                                 keyterms=keyterms)
    client.run_blocking(read_audio, stop)
    return " | ".join(finals) or "(nothing)"


def batch(key: str, pcm: bytes, keyterms: list[str]) -> str:
    from stt.deepgram_batch import DeepgramBatchSTT

    client = DeepgramBatchSTT(key, keyterms=keyterms)
    try:
        return client.transcribe(pcm, SAMPLE_RATE)[0] or "(nothing)"
    finally:
        client.close()


def hits(text: str, expected: tuple[str, ...]) -> int:
    low = text.lower()
    return sum(word in low for word in expected)


def run(extra: list[str], with_batch: bool) -> int:
    """Transcribe every recorded clip without and with keyterms; print a comparison."""
    import main
    from core.config import load_settings

    settings = load_settings(require_key=False)
    key = os.environ.get("DEEPGRAM_API_KEY", "").strip()
    terms = main.speech_keyterms(settings) + [t for t in extra if t]
    print(f"Keyterms ({len(terms)}): {', '.join(terms)}\n")
    totals = {"A": 0, "B": 0, "bA": 0, "bB": 0}
    possible = 0
    for name, text, expected in PHRASES:
        pcm = read_clip(name)
        if pcm is None:
            print(f"[{name}] no recording, skipped (run: python tools/stt_ab.py record)")
            continue
        possible += len(expected)
        without = stream(key, settings, pcm, [])
        with_terms = stream(key, settings, pcm, terms)
        totals["A"] += hits(without, expected)
        totals["B"] += hits(with_terms, expected)
        print(f'[{name}] said: "{text}"')
        print(f"  live, no keyterms : {without}")
        print(f"  live, keyterms    : {with_terms}")
        if with_batch:
            b_without, b_with = batch(key, pcm, []), batch(key, pcm, terms)
            totals["bA"] += hits(b_without, expected)
            totals["bB"] += hits(b_with, expected)
            print(f"  ptt,  no keyterms : {b_without}")
            print(f"  ptt,  keyterms    : {b_with}")
        print()
    if possible:
        print(f"Key words found (Roman spelling): live {totals['A']}/{possible} without, "
              f"{totals['B']}/{possible} with keyterms")
        if with_batch:
            print(f"                                  push-to-talk {totals['bA']}/{possible} "
                  f"without, {totals['bB']}/{possible} with keyterms")
    return 0


def main_cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record", help="record the test phrases")
    rec.add_argument("--only", help="only phrases whose name contains this")
    cmp_ = sub.add_parser("run", help="compare without / with keyterms")
    cmp_.add_argument("--extra", action="append", default=["Arijit Singh"],
                      help="extra keyterm (repeatable; default: Arijit Singh)")
    cmp_.add_argument("--batch", action="store_true", help="also compare push-to-talk")
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    if args.command == "record":
        return record(args.only)
    if not os.environ.get("DEEPGRAM_API_KEY", "").strip():
        print("DEEPGRAM_API_KEY is not set. Add it to .env in the project folder.")
        return 2
    return run(args.extra, args.batch)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to a legacy code page
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main_cli())

"""OCR for the visual adapter: find on-screen text so a ``screen`` target can say
``text="Download"`` instead of raw coordinates.

Engines:

* ``windows``   - built-in Windows.Media.Ocr via the ``winrt`` packages; no install, but only
  the languages Windows offers OCR for (English yes, Hindi no).
* ``tesseract`` - the Tesseract binary (``tesseract`` on PATH or ``ACTIONS_TESSERACT_CMD``);
  reads Devanagari when the ``hin`` language data is installed.

``auto`` uses Tesseract when it is installed and Windows OCR otherwise. Matching is plain and
deterministic: whole-word sequence match within a line, case-insensitive.
"""

from __future__ import annotations

import asyncio
import csv
import io
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from actions.config import ActionSettings

_TESSERACT_LANGS = {"en": "eng", "hi": "hin", "mr": "mar", "bn": "ben", "ta": "tam", "te": "tel", "kn": "kan",
                    "gu": "guj", "pa": "pan", "ml": "mal", "or": "ori"}
_WINDOWS_LANGS = {"en": "en-US"}


@dataclass(frozen=True)
class OcrWord:
    text: str
    left: int
    top: int
    width: int
    height: int
    line: int


@dataclass(frozen=True)
class TextMatch:
    text: str
    left: int
    top: int
    width: int
    height: int

    @property
    def center(self) -> tuple[int, int]:
        return self.left + self.width // 2, self.top + self.height // 2


class OcrEngine(Protocol):
    name: str

    def available(self) -> bool: ...

    def recognize(self, image_path: Path) -> list[OcrWord]: ...


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFC", text).casefold()
    return "".join(ch for ch in text if ch.isalnum() or unicodedata.category(ch).startswith("M"))


def find_text(words: list[OcrWord], query: str) -> list[TextMatch]:
    """Occurrences of ``query`` in reading order. Multi-word queries must appear as consecutive
    words on one line; a single word may also match inside a longer word ("Download:")."""
    wanted = [w for w in (_norm(q) for q in query.split()) if w]
    if not wanted:
        return []
    lines: dict[int, list[OcrWord]] = {}
    for word in words:
        lines.setdefault(word.line, []).append(word)
    matches: list[TextMatch] = []
    for line_words in lines.values():
        line_words.sort(key=lambda w: w.left)
        normed = [_norm(w.text) for w in line_words]
        for i in range(len(line_words) - len(wanted) + 1):
            window = normed[i:i + len(wanted)]
            exact = window == wanted
            partial = len(wanted) == 1 and len(wanted[0]) >= 3 and wanted[0] in window[0]
            if exact or partial:
                hit = line_words[i:i + len(wanted)]
                left, top = min(w.left for w in hit), min(w.top for w in hit)
                right = max(w.left + w.width for w in hit)
                bottom = max(w.top + w.height for w in hit)
                matches.append(TextMatch(" ".join(w.text for w in hit), left, top, right - left, bottom - top))
    matches.sort(key=lambda m: (m.top // 10, m.left))  # rows within ~10 px count as one row
    return matches


class WindowsOcrEngine:
    name = "windows"

    def __init__(self, languages: list[str]) -> None:
        self.languages = languages

    def available(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            from winrt.windows.media.ocr import OcrEngine as _Engine  # noqa: F401
        except ImportError:
            return False
        return True

    def recognize(self, image_path: Path) -> list[OcrWord]:
        # Called on the adapter's worker thread, which has no event loop of its own.
        return asyncio.run(self._recognize(image_path.read_bytes()))

    async def _recognize(self, data: bytes) -> list[OcrWord]:
        from winrt.windows.globalization import Language
        from winrt.windows.graphics.imaging import BitmapDecoder
        from winrt.windows.media.ocr import OcrEngine as _Engine
        from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream)
        writer.write_bytes(data)
        await writer.store_async()
        await writer.flush_async()
        writer.detach_stream()
        stream.seek(0)
        bitmap = await (await BitmapDecoder.create_async(stream)).get_software_bitmap_async()

        engines = []
        for lang in self.languages:
            tag = _WINDOWS_LANGS.get(lang, lang)
            if _Engine.is_language_supported(Language(tag)):
                engines.append(_Engine.try_create_from_language(Language(tag)))
        if not engines:
            engines = [_Engine.try_create_from_user_profile_languages()]

        words: list[OcrWord] = []
        line_no = 0
        for engine in filter(None, engines):
            result = await engine.recognize_async(bitmap)
            for line in result.lines:
                for w in line.words:
                    r = w.bounding_rect
                    words.append(OcrWord(w.text, round(r.x), round(r.y), round(r.width), round(r.height), line_no))
                line_no += 1
        return words


class TesseractOcrEngine:
    name = "tesseract"

    def __init__(self, languages: list[str], command: str | None = None) -> None:
        self.languages = languages
        self.command = command or shutil.which("tesseract")

    def available(self) -> bool:
        return bool(self.command) and Path(self.command).exists()

    def recognize(self, image_path: Path) -> list[OcrWord]:
        langs = "+".join(_TESSERACT_LANGS.get(lang, lang) for lang in self.languages)
        proc = subprocess.run([str(self.command), str(image_path), "stdout", "-l", langs, "tsv"],
                              capture_output=True, timeout=30, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"tesseract failed: {proc.stderr.decode('utf-8', 'replace').strip()[:300]}")
        return parse_tesseract_tsv(proc.stdout.decode("utf-8", "replace"))


def parse_tesseract_tsv(tsv: str) -> list[OcrWord]:
    words = []
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        text = (row.get("text") or "").strip()
        if row.get("level") != "5" or not text or float(row.get("conf") or -1) < 0:
            continue
        line = int(row["block_num"]) * 10_000 + int(row["par_num"]) * 100 + int(row["line_num"])
        words.append(OcrWord(text, int(row["left"]), int(row["top"]), int(row["width"]), int(row["height"]), line))
    return words


def create_ocr_engine(settings: ActionSettings) -> OcrEngine | None:
    tesseract = TesseractOcrEngine(settings.ocr_languages, settings.tesseract_cmd)
    windows = WindowsOcrEngine(settings.ocr_languages)
    order = {"auto": [tesseract, windows], "tesseract": [tesseract], "windows": [windows]}[settings.ocr_engine]
    return next((engine for engine in order if engine.available()), None)

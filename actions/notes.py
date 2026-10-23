"""Write a note, save it in the notes folder (Documents) and open it (PDF "Actions": notes.py).

The file name is made from the title with every path trick removed, and the file
is always written inside ``settings.notes_dir``.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime

from actions import registry
from actions.registry import ActionError, action

MAX_TITLE_CHARS = 80
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def sanitize_filename(title: str) -> str:
    """Make a safe Windows file name (without extension) from a note title.

    Removes path separators, ``..``, reserved characters and control characters,
    and avoids reserved device names such as ``CON`` or ``LPT1``.
    """
    text = unicodedata.normalize("NFC", title)
    text = "".join(" " if unicodedata.category(c).startswith("C") else c for c in text)
    text = re.sub(r'[<>:"/\\|?*]', " ", text)
    text = text.replace("..", " ")
    text = " ".join(text.split()).strip(" .")
    text = text[:MAX_TITLE_CHARS].strip(" .")
    if not text:
        return "note"
    if text.split(".")[0].strip().lower() in _RESERVED_NAMES:
        text = f"note_{text}"
    return text


@action(
    "Save a note, letter or application as a text file in Documents and open it. Write "
    "the COMPLETE text in 'content' in the user's language.",
    {
        "title": {"type": "string", "minLength": 1, "maxLength": MAX_TITLE_CHARS},
        "content": {"type": "string", "minLength": 1, "maxLength": 20000},
    },
    required=["title", "content"],
    risk="medium",
)
def save_note(title: str, content: str) -> str:
    """Write a UTF-8 text file in the notes folder, open it and return its path."""
    if not content:
        raise ActionError("the note is empty")
    if len(content) > registry.limit("note_max_chars", 20000):
        raise ActionError("the note is too long")
    folder = registry.config().notes_dir.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    name = sanitize_filename(title)
    path = folder / f"{name}.txt"
    if path.exists():
        path = folder / f"{name} {datetime.now():%Y%m%d_%H%M%S}.txt"
    if path.resolve().parent != folder:
        raise ActionError("the note name is not allowed")
    path.write_text(content, encoding="utf-8")
    registry._startfile(str(path))
    return f"note saved to {path}"

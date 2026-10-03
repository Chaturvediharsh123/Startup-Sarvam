from __future__ import annotations

import hashlib
import math
from pathlib import Path

import pytest

from memory.config import MemorySettings
from memory.manager import MemoryManager
from memory.sqlite_store import tokenize

# Words that mean the same thing across English / Hinglish / Hindi map to one concept, so the
# fake embedder behaves like a (tiny) multilingual model without downloading anything.
CONCEPTS = {
    "electricity": "electricity", "bijli": "electricity", "बिजली": "electricity", "power": "electricity",
    "bill": "bill", "bills": "bill", "बिल": "bill", "invoice": "bill",
    "download": "download", "downloaded": "download", "डाउनलोड": "download", "save": "download",
    "hindi": "hindi", "हिंदी": "hindi",
    "reply": "reply", "replies": "reply", "answer": "reply", "jawab": "reply",
    "notepad": "notepad", "note": "notepad",
    "weather": "weather", "mausam": "weather",
}


class FakeEmbedder:
    dim = 64

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            vec[0] = 0.1  # small shared component: never a zero vector
            for token in tokenize(text):
                concept = CONCEPTS.get(token)
                if concept is None:
                    continue  # like a real model, filler words barely move the meaning
                bucket = 1 + int(hashlib.md5(concept.encode()).hexdigest(), 16) % (self.dim - 1)
                vec[bucket] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


@pytest.fixture
def settings(tmp_path: Path) -> MemorySettings:
    return MemorySettings(
        db_path=tmp_path / "memory.sqlite3",
        lancedb_path=tmp_path / "lancedb",
        semantic_min_similarity=0.3,
        _env_file=None,
    )


@pytest.fixture
def manager(settings: MemorySettings):
    with MemoryManager(settings, embedder=FakeEmbedder()) as m:
        yield m


@pytest.fixture
def keyword_manager(settings: MemorySettings):
    with MemoryManager(settings, enable_semantic=False) as m:
        yield m

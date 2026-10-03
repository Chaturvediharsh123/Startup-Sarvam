"""LanceDB semantic index with local embeddings.

This is an index, not a source of truth: it stores only (id, text, vector) and can be rebuilt
from SQLite at any time with ``MemoryManager.rebuild_semantic_index``.
"""

from __future__ import annotations

import logging
import math
import re
import threading
from pathlib import Path
from typing import Iterable, Protocol

logger = logging.getLogger("memory.semantic")

KINDS = ("memories", "workflows")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


def _normalize(vector: Iterable[float]) -> list[float]:
    values = [float(x) for x in vector]
    norm = math.sqrt(sum(x * x for x in values)) or 1.0
    return [x / norm for x in values]


class FastEmbedEmbedder:
    """Local ONNX embeddings via fastembed (no PyTorch). The model downloads once to ``cache_dir``."""

    def __init__(self, model_name: str, cache_dir: Path | str | None = None) -> None:
        self.model_name = model_name
        self.cache_dir = Path(cache_dir).expanduser() if cache_dir else None
        self._model = None
        self._lock = threading.Lock()

    def _load(self):  # noqa: ANN202
        with self._lock:
            if self._model is None:
                from fastembed import TextEmbedding

                self._model = TextEmbedding(model_name=self.model_name,
                                            cache_dir=str(self.cache_dir) if self.cache_dir else None)
            return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [_normalize(v) for v in self._load().embed(texts)]


class SemanticStore:
    def __init__(self, path: Path | str, embedder: Embedder) -> None:
        import lancedb

        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.embedder = embedder
        self._db = lancedb.connect(str(self.path))
        self._tables: dict[str, object] = {}
        self._dim: int | None = None
        self._lock = threading.RLock()

    def _table(self, kind: str):  # noqa: ANN202
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        with self._lock:
            if kind in self._tables:
                return self._tables[kind]
            import pyarrow as pa

            if self._dim is None:
                self._dim = len(self.embedder.embed(["dimension probe"])[0])
            schema = pa.schema([
                pa.field("id", pa.string()),
                pa.field("text", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), self._dim)),
            ])
            table = self._db.create_table(kind, schema=schema, exist_ok=True)
            if table.schema.field("vector").type.list_size != self._dim:
                # Embedding model changed. The index is disposable (SQLite is the source of truth).
                logger.warning("embedding dimension changed for %s; recreating index - run rebuild_semantic_index()", kind)
                self._db.drop_table(kind)
                table = self._db.create_table(kind, schema=schema)
            self._tables[kind] = table
            return table

    @staticmethod
    def _check_id(item_id: str) -> str:
        if not _SAFE_ID.match(item_id):
            raise ValueError(f"unsafe id {item_id!r}")
        return item_id

    def upsert(self, kind: str, item_id: str, text: str) -> None:
        self.upsert_many(kind, [(item_id, text)])

    def upsert_many(self, kind: str, items: list[tuple[str, str]]) -> None:
        if not items:
            return
        vectors = self.embedder.embed([text for _, text in items])
        rows = [{"id": self._check_id(i), "text": t, "vector": v} for (i, t), v in zip(items, vectors)]
        with self._lock:
            table = self._table(kind)
            table.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(rows)

    def delete(self, kind: str, item_id: str) -> None:
        with self._lock:
            self._table(kind).delete(f"id = '{self._check_id(item_id)}'")

    def clear(self, kind: str) -> None:
        with self._lock:
            self._table(kind).delete("true")

    def search(self, kind: str, query: str, limit: int) -> list[tuple[str, float]]:
        """(id, cosine similarity) pairs, most similar first."""
        vector = self.embedder.embed([query])[0]
        with self._lock:
            table = self._table(kind)
            if table.count_rows() == 0:
                return []
            rows = table.search(vector).distance_type("cosine").limit(limit).select(["id"]).to_list()
        return [(r["id"], 1.0 - float(r["_distance"])) for r in rows]

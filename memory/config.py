"""Memory Block configuration (environment variables prefixed ``MEMORY_`` or a ``.env`` file)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RankingWeights(BaseModel):
    """Weights of the deterministic ranking (see memory.retrieval). Tune with memory.evaluation;
    override via MEMORY_RANKING='{"agreement_bonus": 0.3}'."""

    agreement_bonus: float = Field(default=0.25, ge=0, le=1, description="Boost when keyword and semantic agree")
    confidence_weight: float = Field(default=0.5, ge=0, le=1, description="score *= (1 - w) + w * confidence")
    memory_freshness: dict[str, float] = Field(default_factory=lambda: {
        "verified": 1.0, "unverified": 0.9, "stale": 0.7, "expired": 0.4, "invalid": 0.0})
    workflow_freshness: dict[str, float] = Field(default_factory=lambda: {
        "current": 1.0, "unverified": 0.8, "stale": 0.7, "failing": 0.5, "expired": 0.4, "invalid": 0.0})


class MemorySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MEMORY_", env_file=".env", extra="ignore")

    db_path: Path = Path(".data/memory/memory.sqlite3")
    lancedb_path: Path = Path(".data/memory/lancedb")

    semantic_enabled: bool = True
    # Multilingual so Hindi, Hinglish and English queries land near each other.
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    # Outside the project folder by default, so a ~220 MB model is not synced/committed.
    embedding_cache_dir: Path = Path("~/.cache/sarvam-memory/models")

    stale_after_days: int = Field(default=90, ge=1)
    workflow_stale_after_days: int = Field(default=30, ge=1)

    default_limit: int = Field(default=5, ge=1, le=50)
    candidate_pool: int = Field(default=30, ge=5, le=200)
    # Defaults tuned with memory.evaluation on tests/memory/data/retrieval_eval.json (recall@3 0.79 -> 0.86).
    min_relevance: float = Field(default=0.2, ge=0, le=1)
    # Cosine similarity below which a semantic hit counts as unrelated (model dependent).
    semantic_min_similarity: float = Field(default=0.3, ge=-1, lt=1)
    ranking: RankingWeights = Field(default_factory=RankingWeights)

    # Opt-in JSONL log of searches (+ planner feedback) for tuning on real data. Queries are
    # passed through the sensitive-data filter first. Kept local; never commit it.
    retrieval_log_path: Path | None = None

    working_max_items: int = Field(default=20, ge=1, le=500)

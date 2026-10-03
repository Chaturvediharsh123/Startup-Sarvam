"""Retrieval evaluation and weight tuning.

A dataset is a set of memories plus queries labelled with the memories they should find
(``expected: []`` means nothing in memory is relevant and the search should come back empty).
Metrics:

* recall@k   - positive queries with at least one expected memory in the top k
* mrr        - mean reciprocal rank of the first expected memory (positive queries)
* rejection  - negative queries that correctly returned nothing
* noise      - share of returned results that were not expected (all queries)
* objective  - 0.4 recall + 0.3 mrr + 0.2 rejection + 0.1 (1 - noise), used to rank configs

Usage::

    python -m memory.evaluation tests/memory/data/retrieval_eval.json --tune
    python -m memory.evaluation --from-log .data/retrieval_log.jsonl   # real cases (needs the real DB)
"""

from __future__ import annotations

import argparse
import itertools
import json
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, Field

from memory.config import MemorySettings
from memory.manager import MemoryManager


class EvalCase(BaseModel):
    query: str
    expected: list[str] = Field(default_factory=list, description="Memory keys (dataset) or ids (log); [] = negative")
    language: str = "unknown"


class EvalDataset(BaseModel):
    memories: list[dict[str, Any]] = Field(description="Memory payloads, each with an extra 'key'")
    cases: list[EvalCase]


class CaseResult(BaseModel):
    query: str
    language: str
    expected: list[str]
    returned: list[str]
    rank: int | None = None  # 1-based rank of the first expected hit


class EvalReport(BaseModel):
    k: int
    recall_at_k: float
    mrr: float
    rejection: float
    noise: float
    objective: float
    by_language: dict[str, dict[str, float]]
    cases: list[CaseResult]

    def failures(self) -> list[CaseResult]:
        return [c for c in self.cases if (c.expected and c.rank is None) or (not c.expected and c.returned)]

    def summary(self) -> str:
        lines = [f"recall@{self.k}={self.recall_at_k:.2f} mrr={self.mrr:.2f} rejection={self.rejection:.2f} "
                 f"noise={self.noise:.2f} objective={self.objective:.3f}"]
        for lang, m in sorted(self.by_language.items()):
            lines.append(f"  {lang:10} " + " ".join(f"{k}={v:.2f}" for k, v in m.items()))
        for c in self.failures():
            lines.append(f"  MISS [{c.language}] {c.query!r}: expected {c.expected or 'nothing'}, got {c.returned}")
        return "\n".join(lines)


def load_dataset(manager: MemoryManager, dataset: EvalDataset) -> dict[str, str]:
    """Save the dataset's memories; returns key -> memory id."""
    ids = {}
    for payload in dataset.memories:
        data = dict(payload)
        key = data.pop("key")
        ids[key] = manager.save(data).id
    return ids


def _metrics(results: list[CaseResult]) -> dict[str, float]:
    positives = [r for r in results if r.expected]
    negatives = [r for r in results if not r.expected]
    returned = sum(len(r.returned) for r in results)
    unexpected = sum(len([x for x in r.returned if x not in r.expected]) for r in results)
    recall = sum(r.rank is not None for r in positives) / len(positives) if positives else 1.0
    mrr = sum(1 / r.rank for r in positives if r.rank) / len(positives) if positives else 1.0
    rejection = sum(not r.returned for r in negatives) / len(negatives) if negatives else 1.0
    noise = unexpected / returned if returned else 0.0
    return {"recall": recall, "mrr": mrr, "rejection": rejection, "noise": noise,
            "objective": 0.4 * recall + 0.3 * mrr + 0.2 * rejection + 0.1 * (1 - noise)}


def evaluate(manager: MemoryManager, cases: list[EvalCase], *, k: int = 3,
             id_map: dict[str, str] | None = None) -> EvalReport:
    id_map = id_map or {}
    to_key = {v: key for key, v in id_map.items()}
    results = []
    for case in cases:
        returned = [to_key.get(h.memory.id, h.memory.id) for h in manager.search(case.query, limit=k)]
        rank = next((i + 1 for i, r in enumerate(returned) if r in case.expected), None)
        results.append(CaseResult(query=case.query, language=case.language, expected=case.expected,
                                  returned=returned, rank=rank))
    overall = _metrics(results)
    by_language = {lang: {name: round(v, 3) for name, v in _metrics([r for r in results if r.language == lang]).items()
                          if name != "objective"}
                   for lang in sorted({r.language for r in results})}
    return EvalReport(k=k, recall_at_k=round(overall["recall"], 4), mrr=round(overall["mrr"], 4),
                      rejection=round(overall["rejection"], 4), noise=round(overall["noise"], 4),
                      objective=round(overall["objective"], 4), by_language=by_language, cases=results)


@contextmanager
def _override(settings: MemorySettings, params: dict[str, float]) -> Iterator[None]:
    """Temporarily set settings such as 'min_relevance' or 'ranking.agreement_bonus'."""
    saved = []
    try:
        for name, value in params.items():
            obj = settings
            *path, attr = name.split(".")
            for part in path:
                obj = getattr(obj, part)
            saved.append((obj, attr, getattr(obj, attr)))
            setattr(obj, attr, value)
        yield
    finally:
        for obj, attr, value in reversed(saved):
            setattr(obj, attr, value)


DEFAULT_GRID: dict[str, list[float]] = {
    "min_relevance": [0.2, 0.25, 0.3, 0.35, 0.4],
    "semantic_min_similarity": [0.3, 0.35, 0.4, 0.45],
    "ranking.agreement_bonus": [0.0, 0.25, 0.5],
}


def tune(manager: MemoryManager, cases: list[EvalCase], *, grid: dict[str, list[float]] | None = None, k: int = 3,
         id_map: dict[str, str] | None = None) -> list[tuple[dict[str, float], EvalReport]]:
    """Evaluate every combination in ``grid``; best first. Embeddings are reused, so this is
    cheap: only query-time ranking parameters belong in the grid."""
    grid = grid or DEFAULT_GRID
    results = []
    for values in itertools.product(*grid.values()):
        params = dict(zip(grid, values))
        with _override(manager.settings, params):
            results.append((params, evaluate(manager, cases, k=k, id_map=id_map)))
    results.sort(key=lambda pr: (-pr[1].objective, -pr[1].recall_at_k, -pr[1].rejection))
    return results


def cases_from_log(path: Path | str) -> list[EvalCase]:
    """Turn logged searches that got planner feedback into evaluation cases (ids, not keys)."""
    searches: dict[str, str] = {}
    cases = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        if entry.get("event") == "search":
            searches[entry["search_id"]] = entry["query"]
        elif entry.get("event") == "feedback" and entry.get("search_id") in searches:
            cases.append(EvalCase(query=searches[entry["search_id"]], expected=entry.get("used") or [], language="logged"))
    return cases


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dataset", nargs="?", help="JSON dataset (memories + cases), loaded into a temporary store")
    parser.add_argument("--from-log", help="retrieval log JSONL; evaluates against the configured real memory DB")
    parser.add_argument("--tune", action="store_true", help="grid-search ranking parameters")
    parser.add_argument("-k", type=int, default=3)
    args = parser.parse_args(argv)

    if args.from_log:
        with MemoryManager() as manager:
            _report(manager, cases_from_log(args.from_log), args, None)
        return
    if not args.dataset:
        parser.error("give a dataset or --from-log")
    dataset = EvalDataset.model_validate_json(Path(args.dataset).read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        settings = MemorySettings(db_path=Path(tmp) / "eval.sqlite3", lancedb_path=Path(tmp) / "lance")
        with MemoryManager(settings) as manager:
            _report(manager, dataset.cases, args, load_dataset(manager, dataset))


def _report(manager: MemoryManager, cases: list[EvalCase], args: argparse.Namespace, id_map: dict[str, str] | None) -> None:
    print("current settings:\n" + evaluate(manager, cases, k=args.k, id_map=id_map).summary())
    if args.tune:
        best = tune(manager, cases, k=args.k, id_map=id_map)
        print("\ntop configurations:")
        for params, report in best[:5]:
            print(f"  {params} -> objective={report.objective:.3f} recall={report.recall_at_k:.2f} "
                  f"mrr={report.mrr:.2f} rejection={report.rejection:.2f} noise={report.noise:.2f}")


if __name__ == "__main__":
    main()

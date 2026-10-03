import json
from pathlib import Path

import pytest

from memory.config import MemorySettings
from memory.evaluation import EvalCase, EvalDataset, cases_from_log, evaluate, load_dataset, tune
from memory.manager import MemoryManager

from .conftest import FakeEmbedder

DATASET = Path(__file__).parent / "data" / "retrieval_eval.json"

SMALL = EvalDataset(
    memories=[
        {"key": "bill", "type": "episode", "source": "execution", "content": "Downloaded the electricity bill"},
        {"key": "music", "type": "preference", "source": "user", "content": "Likes weather updates"},
    ],
    cases=[
        EvalCase(query="बिजली का बिल", expected=["bill"], language="hindi"),
        EvalCase(query="mausam", expected=["music"], language="hinglish"),
        EvalCase(query="volume badhao", expected=[], language="negative"),
    ],
)


def test_evaluate_reports_metrics_per_language(manager):
    ids = load_dataset(manager, SMALL)
    report = evaluate(manager, SMALL.cases, id_map=ids)
    assert report.recall_at_k == 1.0 and report.mrr == 1.0 and report.rejection == 1.0
    assert set(report.by_language) == {"hindi", "hinglish", "negative"}
    assert report.failures() == []
    assert "recall@3=1.00" in report.summary()


def test_tune_restores_settings_and_ranks_configs(manager):
    ids = load_dataset(manager, SMALL)
    before = (manager.settings.min_relevance, manager.settings.ranking.agreement_bonus)
    results = tune(manager, SMALL.cases, grid={"min_relevance": [0.0, 0.99], "ranking.agreement_bonus": [0.0, 0.5]},
                   id_map=ids)
    assert len(results) == 4
    assert results[0][1].objective >= results[-1][1].objective
    assert results[-1][0]["min_relevance"] == 0.99  # filters everything out
    assert (manager.settings.min_relevance, manager.settings.ranking.agreement_bonus) == before


def test_ranking_weights_are_configurable(settings):
    settings.ranking.confidence_weight = 0.0  # confidence no longer affects the score
    with MemoryManager(settings, embedder=FakeEmbedder()) as mgr:
        mgr.save({"type": "fact", "content": "JVVNL portal", "source": "agent", "confidence": 0.1})
        mgr.save({"type": "fact", "content": "JVVNL portal", "source": "agent", "confidence": 0.9})
        scores = [h.score for h in mgr.search("JVVNL portal")]
        assert scores[0] == scores[1]


def test_retrieval_log_and_feedback_become_cases(tmp_path):
    log = tmp_path / "retrieval.jsonl"
    settings = MemorySettings(db_path=tmp_path / "m.sqlite3", lancedb_path=tmp_path / "l",
                              retrieval_log_path=log, _env_file=None)
    with MemoryManager(settings, embedder=FakeEmbedder()) as mgr:
        m = mgr.save({"type": "fact", "content": "Electricity provider is JVVNL", "source": "user"})
        hits = mgr.search("JVVNL OTP 482913")
        assert hits[0].search_id
        mgr.record_search_feedback(hits[0].search_id, [m.id])
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [entry["event"] for entry in lines] == ["search", "feedback"]
    assert "482913" not in log.read_text(encoding="utf-8")  # queries are filtered too
    cases = cases_from_log(log)
    assert cases[0].expected == [m.id] and "JVVNL" in cases[0].query


def test_logging_is_off_by_default(manager):
    manager.save({"type": "fact", "content": "JVVNL", "source": "user"})
    assert manager.search("JVVNL")[0].search_id is None


@pytest.mark.integration
def test_retrieval_quality_does_not_regress(tmp_path):
    """Real model on the bundled dataset. Thresholds sit just under the measured values
    (recall@3 0.86, rejection 0.83 on 2026-10-03)."""
    dataset = EvalDataset.model_validate_json(DATASET.read_text(encoding="utf-8"))
    settings = MemorySettings(db_path=tmp_path / "m.sqlite3", lancedb_path=tmp_path / "l", _env_file=None)
    with MemoryManager(settings) as mgr:
        report = evaluate(mgr, dataset.cases, id_map=load_dataset(mgr, dataset))
    print(report.summary())
    assert report.recall_at_k >= 0.82 and report.rejection >= 0.8 and report.noise <= 0.3
    assert report.by_language["english"]["recall"] == 1.0

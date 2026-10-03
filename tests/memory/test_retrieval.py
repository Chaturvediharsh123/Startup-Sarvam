from datetime import timedelta

import pytest

from memory.manager import MemoryManager
from memory.models import Freshness, utcnow
from memory.retrieval import keyword_score, query_terms

from .conftest import FakeEmbedder


def test_query_terms_drop_stopwords_in_all_three_languages():
    assert query_terms("How did I download my electricity bill last time?") == ["download", "electricity", "bill"]
    assert query_terms("pichli baar bijli ka bill kaise download kiya") == ["bijli", "bill", "download", "kiya"]
    assert query_terms("मेरा बिजली का बिल") == ["बिजली", "बिल"]
    assert query_terms("the") == ["the"]  # nothing meaningful left: fall back to all tokens


def test_keyword_score_is_term_coverage():
    assert keyword_score(["jvvnl"], "Provider JVVNL") == 1.0
    assert keyword_score(["bill", "download"], "bills page") == 0.5
    assert keyword_score(["x"], "") == 0.0


def test_exact_identifier_retrieval(keyword_manager):
    keyword_manager.save({"type": "fact", "content": "Electricity provider is JVVNL", "source": "user"})
    keyword_manager.save({"type": "fact", "content": "Water provider is PHED", "source": "user"})
    hits = keyword_manager.search("JVVNL")
    assert len(hits) == 1 and "JVVNL" in hits[0].memory.content
    assert hits[0].keyword_score == 1.0 and hits[0].semantic_score is None


def test_semantic_retrieval_across_languages(manager):
    manager.save({"type": "episode", "content": "Downloaded the electricity bill PDF from the provider portal",
                  "source": "execution"})
    manager.save({"type": "preference", "content": "Weather updates in the morning", "source": "user"})
    hits = manager.search("बिजली का बिल")
    assert hits and "electricity bill" in hits[0].memory.content
    assert hits[0].semantic_score and hits[0].semantic_score > 0
    assert all("Weather" not in h.memory.content for h in hits)  # relevance filter


def test_irrelevant_results_are_filtered_not_concatenated(manager):
    manager.save({"type": "fact", "content": "Electricity provider is JVVNL", "source": "user"})
    assert manager.search("mausam kaisa hai") == []


def test_ranking_prefers_verified_and_confident(manager):
    now = utcnow()
    stale = manager.save({"type": "fact", "content": "Electricity bill is paid on the JVVNL portal", "source": "agent",
                          "created_at": now - timedelta(days=400)})
    fresh = manager.save({"type": "fact", "content": "Electricity bill is paid on the JVVNL portal", "source": "user"})
    hits = manager.search("JVVNL electricity bill")
    assert [h.memory.id for h in hits[:2]] == [fresh.id, stale.id]
    assert hits[0].is_confirmed and hits[0].caveat is None
    assert hits[1].freshness is Freshness.STALE and not hits[1].is_confirmed
    assert "never verified" in hits[1].caveat


def test_expired_memories_are_marked_or_excluded(manager):
    m = manager.save({"type": "fact", "content": "JVVNL portal is under maintenance", "source": "execution",
                      "expires_at": utcnow() - timedelta(hours=1)})
    hits = manager.search("JVVNL portal")
    assert hits[0].memory.id == m.id and hits[0].freshness is Freshness.EXPIRED
    assert "historical" in hits[0].caveat
    assert manager.search("JVVNL portal", include_expired=False) == []


def test_invalid_memories_are_excluded_by_default(manager):
    m = manager.save({"type": "fact", "content": "JVVNL consumer number is 12345", "source": "agent"})
    manager.invalidate(m.id, reason="user corrected it")
    assert manager.search("JVVNL consumer number") == []
    assert manager.search("JVVNL consumer number", include_invalid=True)[0].freshness is Freshness.INVALID


def test_type_and_tag_filters(manager):
    manager.save({"type": "preference", "content": "Prefers Hindi replies", "source": "user", "metadata": {"tags": ["voice"]}})
    manager.save({"type": "fact", "content": "Hindi is the first language", "source": "user"})
    assert {h.memory.type.value for h in manager.search("Hindi", types=["preference"])} == {"preference"}
    assert len(manager.search("Hindi", tags=["VOICE"])) == 1


def test_retrieval_is_not_verification(manager):
    m = manager.save({"type": "fact", "content": "JVVNL bills arrive monthly", "source": "agent"})
    for _ in range(3):
        manager.search("JVVNL bills")
        manager.build_context("JVVNL bills")
    assert manager.get(m.id).last_verified_at is None
    assert manager.verification_history(m.id) == []


def test_sqlite_is_the_source_of_truth_for_orphan_vectors(manager):
    m = manager.save({"type": "fact", "content": "Electricity bill portal", "source": "user"})
    manager._store.delete_memory(m.id)  # simulate an index left behind
    assert manager.search("electricity bill") == []


def test_rebuild_semantic_index(settings):
    with MemoryManager(settings, embedder=FakeEmbedder()) as mgr:
        mgr.save({"type": "fact", "content": "Electricity bill portal", "source": "user"})
        mgr._semantic.clear("memories")
        assert mgr.search("बिजली") == []
        assert mgr.rebuild_semantic_index() == {"memories": 1, "workflows": 0}
        assert len(mgr.search("बिजली")) == 1


def test_context_for_the_planner(manager):
    manager.save({"type": "preference", "content": "Prefers Hindi replies", "source": "user"})
    manager.save({"type": "fact", "content": "Hindi keyboard installed", "source": "agent"})
    manager.working.set_task("reply in Hindi")
    manager.working.add_turn("user", "Hindi mein jawab do")
    ctx = manager.build_context("Hindi replies")
    text = ctx.to_prompt()
    assert "Current task: reply in Hindi" in text
    assert "Prefers Hindi replies" in text
    assert "not guaranteed truth" in text
    assert "inferred" in text  # caveat on the agent-sourced memory


@pytest.mark.integration
def test_real_multilingual_embeddings(tmp_path):
    """Downloads/uses the configured local model (~220 MB, cached outside the repo)."""
    from memory.config import MemorySettings

    settings = MemorySettings(db_path=tmp_path / "m.sqlite3", lancedb_path=tmp_path / "lance", _env_file=None)
    with MemoryManager(settings) as mgr:
        assert mgr.semantic_enabled
        bill = mgr.save({"type": "episode", "source": "execution",
                         "content": "Downloaded the electricity bill PDF from the JVVNL website"})
        mgr.save({"type": "preference", "source": "user", "content": "Likes devotional music in the morning"})
        for query in ("बिजली का बिल कैसे डाउनलोड किया था", "How did I download my electricity bill last time?",
                      "bijli ka bill"):
            hits = mgr.search(query)
            assert hits and hits[0].memory.id == bill.id, (query, [(h.memory.content, h.score) for h in hits])
        assert all("devotional" not in h.memory.content for h in mgr.search("बिजली का बिल"))

import sqlite3

import pytest

from memory.models import Memory, MemoryNotFoundError, MemoryVerification, utcnow
from memory.sqlite_store import SQLiteStore, tokenize
from memory.workflow import Workflow, WorkflowVerification


def make_workflow(**kw):
    return Workflow(**{
        "goal": "Download electricity bill",
        "steps": [{"order": 1, "description": "Open portal", "action_type": "open_url",
                   "parameters": {"url": "https://bills.example"}}],
        "verification_history": [WorkflowVerification(execution_id="exec_1", success=True, workflow_version=1)],
        **kw,
    })


@pytest.fixture
def store(tmp_path):
    s = SQLiteStore(tmp_path / "m.sqlite3")
    yield s
    s.close()


def test_tokenize_keeps_devanagari_words_whole():
    assert tokenize("बिजली का बिल, JVVNL portal!") == ["बिजली", "का", "बिल", "jvvnl", "portal"]


def test_memory_crud(store):
    m = Memory(type="fact", content={"provider": "JVVNL"}, source="user")
    store.insert_memory(m)
    assert store.get_memory(m.id) == m
    with pytest.raises(ValueError):
        store.insert_memory(m)
    store.replace_memory(m.model_copy(update={"content": "changed"}))
    assert store.get_memory(m.id).content == "changed"
    assert store.delete_memory(m.id) is True
    assert store.get_memory(m.id) is None
    assert store.delete_memory(m.id) is False
    with pytest.raises(MemoryNotFoundError):
        store.replace_memory(m)


def test_fts_exact_and_hindi_matching(store):
    a = Memory(type="fact", content="बिजली का बिल JVVNL से आता है", source="user")
    b = Memory(type="fact", content="बिजली कटौती शाम को होती है", source="user")  # power cut, no bill
    for m in (a, b):
        store.insert_memory(m)
    assert {i for i, _ in store.keyword_search("memories", ["jvvnl"], 10)} == {a.id}
    assert {i for i, _ in store.keyword_search("memories", ["बिल"], 10)} == {a.id}  # not a prefix-fragment of बिजली
    assert {i for i, _ in store.keyword_search("memories", ["बिजली"], 10)} == {a.id, b.id}
    assert store.keyword_search("memories", ['quote" OR 1'], 10) == []  # quoting is escaped


def test_fts_prefix_matching(store):
    m = Memory(type="episode", content="Downloaded the bills from the portal", source="execution")
    store.insert_memory(m)
    assert store.keyword_search("memories", ["bill"], 10)[0][0] == m.id


def test_persistence_and_cascade(tmp_path):
    path = tmp_path / "p.sqlite3"
    s = SQLiteStore(path)
    m = Memory(type="preference", content="Hindi replies", source="user")
    s.insert_memory(m)
    s.add_memory_verification(MemoryVerification(memory_id=m.id, verified_at=utcnow(), method="user_confirmation"))
    s.close()

    s = SQLiteStore(path)
    assert s.get_memory(m.id).content == "Hindi replies"
    assert len(s.memory_verifications(m.id)) == 1
    s.delete_memory(m.id)
    assert s.memory_verifications(m.id) == []
    s.close()


def test_verification_requires_existing_memory(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.add_memory_verification(MemoryVerification(memory_id="mem_missing", verified_at=utcnow(), method="x"))


def test_workflow_round_trip_with_history_and_versions(store):
    w = make_workflow()
    store.insert_workflow(w)
    loaded = store.get_workflow(w.id)
    assert loaded.goal == w.goal and len(loaded.verification_history) == 1
    store.add_workflow_verification(w.id, WorkflowVerification(execution_id="exec_2", success=False, workflow_version=1))
    assert [v.success for v in store.get_workflow(w.id).verification_history] == [True, False]

    v2 = loaded.model_copy(update={"version": 2, "goal": "Download latest electricity bill"})
    store.replace_workflow(v2, archive=loaded)
    assert store.get_workflow(w.id).version == 2
    assert [old.version for old in store.workflow_versions(w.id)] == [1]
    assert store.keyword_search("workflows", ["latest"], 5)[0][0] == w.id

    assert store.delete_workflow(w.id)
    assert store.get_workflow(w.id) is None and store.workflow_versions(w.id) == []

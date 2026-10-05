"""Tests for the SQLite conversation memory."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from assistant.memory import SCHEMA_VERSION, Memory, migrate


@pytest.fixture
def mem() -> Memory:
    with Memory(":memory:", language="hi-IN") as m:
        yield m


def test_recent_messages_oldest_first_and_limited(mem: Memory) -> None:
    for i in range(5):
        mem.add_message("user", f"u{i}")
        mem.add_message("assistant", f"a{i}")
    recent = mem.recent_messages(limit=4)
    assert [m["content"] for m in recent] == ["u3", "a3", "u4", "a4"]
    assert recent[0] == {"role": "user", "content": "u3"}
    assert mem.recent_messages(limit=0) == []


def test_invalid_role_rejected(mem: Memory) -> None:
    with pytest.raises(ValueError):
        mem.add_message("system", "x")


def test_recent_messages_per_session(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    with Memory(db) as first:
        first.add_message("user", "old session")
    with Memory(db) as second:
        second.add_message("user", "new session")
        assert [m["content"] for m in second.recent_messages()] == ["new session"]
        assert [m["content"] for m in second.recent_messages(all_sessions=True)] == [
            "old session",
            "new session",
        ]


def test_search_across_sessions(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    with Memory(db) as first:
        first.add_message("user", "Chrome kholo")
    with Memory(db) as second:
        second.add_message("user", "Notepad kholo")
        second.add_message("user", "100% sure_thing")
        results = second.search_messages("kholo")
        assert {r["content"] for r in results} == {"Chrome kholo", "Notepad kholo"}
        assert len(second.search_messages("100%")) == 1
        assert second.search_messages("0%s") == []


def test_last_action_ignores_failures(mem: Memory) -> None:
    assert mem.last_action() is None
    mem.log_action("open_app", {"name": "chrome"}, "Opened chrome", "ok", 12)
    mem.log_action("close_app", {"name": "chrome"}, "denied", "denied")
    mem.log_action("open_app", {"name": "paint"}, "boom", "error")
    mem.log_action("format_disk", {}, "blocked", "blocked")
    last = mem.last_action()
    assert last is not None
    assert last["name"] == "open_app"
    assert last["args"] == {"name": "chrome"}


def test_invalid_action_status_rejected(mem: Memory) -> None:
    with pytest.raises(ValueError):
        mem.log_action("x", {}, "", "maybe")


def test_facts_upsert_and_case_insensitive(mem: Memory) -> None:
    mem.remember("Name", "Harsh")
    mem.remember("  NAME ", "Harsh Chaturvedi")
    assert mem.recall("name") == "Harsh Chaturvedi"
    assert mem.all_facts() == {"name": "Harsh Chaturvedi"}
    assert mem.forget("NaMe") is True
    assert mem.forget("name") is False
    assert mem.recall("name") is None
    with pytest.raises(ValueError):
        mem.remember("  ", "x")


def test_unicode_round_trip(mem: Memory) -> None:
    hindi = "मेरा नाम हर्ष है"
    tamil = "என் பெயர் ஹர்ஷ்"
    mem.add_message("user", hindi, "hi-IN")
    mem.add_message("user", tamil, "ta-IN")
    mem.remember("शहर", "दिल्ली")
    mem.log_action("type_text", {"text": hindi}, "typed", "ok")
    assert [m["content"] for m in mem.recent_messages()] == [hindi, tamil]
    assert mem.recall("शहर") == "दिल्ली"
    assert mem.last_action()["args"] == {"text": hindi}  # type: ignore[index]
    assert mem.search_messages("பெயர்")[0]["content"] == tamil


def test_context_note(mem: Memory) -> None:
    assert mem.context_note() == ""
    mem.remember("name", "Harsh")
    mem.log_action("open_app", {"name": "chrome"}, "Opened chrome", "ok")
    note = mem.context_note()
    assert "name: Harsh" in note
    assert "open_app" in note and "chrome" in note


def test_clear_session_keeps_facts(mem: Memory) -> None:
    mem.add_message("user", "hello")
    mem.log_action("open_app", {"name": "chrome"}, "ok", "ok")
    mem.remember("name", "Harsh")
    mem.clear_session()
    assert mem.recent_messages() == []
    assert mem.last_action() is None
    assert mem.recall("name") == "Harsh"


def test_concurrent_writes(tmp_path: Path) -> None:
    errors: list[BaseException] = []
    with Memory(tmp_path / "c.db") as m:

        def worker(n: int) -> None:
            try:
                for i in range(40):
                    m.add_message("user", f"t{n}-{i}")
                    m.log_action("noop", {"i": i}, "ok", "ok")
                    m.remember(f"k{n}", str(i))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert len(m.recent_messages(limit=1000)) == 200
        assert len(m.all_facts()) == 5


def test_close_twice_and_use_after_close() -> None:
    m = Memory(":memory:")
    m.close()
    m.close()
    with pytest.raises(RuntimeError):
        m.add_message("user", "x")


def test_session_end_recorded(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    with Memory(db):
        pass
    conn = sqlite3.connect(db)
    ended = conn.execute("SELECT ended_at FROM sessions").fetchone()[0]
    conn.close()
    assert ended


def test_migration_on_existing_db(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    with Memory(db) as m:
        m.remember("name", "Harsh")
        m.add_message("user", "keep me")
    # Re-opening runs migrate again: no data lost, version recorded once.
    with Memory(db) as m:
        assert m.recall("name") == "Harsh"
        assert m.recent_messages(all_sessions=True)[0]["content"] == "keep me"
    conn = sqlite3.connect(db)
    versions = [r[0] for r in conn.execute("SELECT version FROM schema_version")]
    assert versions == [SCHEMA_VERSION]
    conn.close()


def test_migration_runs_new_steps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import assistant.memory as memory_mod

    db = tmp_path / "v.db"
    with Memory(db) as m:
        m.remember("city", "Delhi")
    monkeypatch.setitem(
        memory_mod.MIGRATIONS, 2, lambda c: c.execute("ALTER TABLE facts ADD COLUMN source TEXT")
    )
    conn = sqlite3.connect(db)
    assert migrate(conn, target=2) == 2
    cols = [r[1] for r in conn.execute("PRAGMA table_info(facts)")]
    assert "source" in cols
    assert conn.execute("SELECT value FROM facts WHERE key='city'").fetchone()[0] == "Delhi"
    conn.close()

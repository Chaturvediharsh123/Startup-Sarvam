"""Schema checks for the golden utterance set and the scorer. Never calls an LLM."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import pytest
import yaml

from tools.golden_eval import (
    APPS,
    FREE_TEXT_KEYS,
    GOLDEN_FILE,
    LANG_CODES,
    SCROLL_AMOUNTS,
    SCROLL_DIRECTIONS,
    SHORTCUTS,
    TOOL_ARGS,
    VOLUME_DIRECTIONS,
    GoldenItem,
    evaluate,
    filter_items,
    load_items,
    match_args,
    score,
)

REQUIRED_FIELDS = {"id", "lang", "text", "expect_tool", "expect_args", "tags"}
OPTIONAL_FIELDS = {"history"}
MIN_PER_LANGUAGE = {
    "hi": 30, "hinglish": 30, "en": 30, "ta": 15, "te": 15, "bn": 3, "kn": 3, "mr": 3,
}

ITEMS = load_items()


@pytest.fixture(scope="module")
def raw_items() -> list[dict[str, Any]]:
    return yaml.safe_load(GOLDEN_FILE.read_text(encoding="utf-8"))["items"]


def test_at_least_150_items() -> None:
    assert len(ITEMS) >= 150


def test_fields(raw_items: list[dict[str, Any]]) -> None:
    for raw in raw_items:
        keys = set(raw)
        missing = REQUIRED_FIELDS - keys
        extra = keys - REQUIRED_FIELDS - OPTIONAL_FIELDS
        assert not missing, f"{raw.get('id')}: missing {missing}"
        assert not extra, f"{raw['id']}: extra {extra}"
        assert isinstance(raw["text"], str) and raw["text"].strip()
        assert isinstance(raw["expect_args"], dict)
        assert isinstance(raw["tags"], list) and raw["tags"]
        if "history" in raw:
            assert isinstance(raw["history"], list) and raw["history"]
            assert all(isinstance(h, str) and h.strip() for h in raw["history"])


def test_ids_unique_and_prefixed() -> None:
    ids = [item.id for item in ITEMS]
    dupes = [i for i, n in Counter(ids).items() if n > 1]
    assert not dupes, f"duplicate ids: {dupes}"
    prefixes = {"hi", "hg", "en", "ta", "te", "bn", "kn", "mr", "fu"}
    assert all(i.split("-")[0] in prefixes for i in ids)


def test_every_language_present() -> None:
    counts = Counter(item.lang for item in ITEMS)
    assert set(counts) <= set(LANG_CODES), f"unknown languages: {set(counts) - set(LANG_CODES)}"
    for lang, minimum in MIN_PER_LANGUAGE.items():
        assert counts[lang] >= minimum, f"{lang}: {counts[lang]} < {minimum}"


def test_every_action_and_plain_qa_covered() -> None:
    tools = {item.expect_tool for item in ITEMS}
    assert set(TOOL_ARGS) <= tools, f"uncovered actions: {set(TOOL_ARGS) - tools}"
    assert None in tools, "need plain Q&A items with expect_tool: null"
    assert tools - {None} <= set(TOOL_ARGS), f"unknown tools: {tools - {None} - set(TOOL_ARGS)}"


def test_core_actions_in_main_languages() -> None:
    for lang in ("hi", "hinglish", "en", "ta", "te"):
        tools = {i.expect_tool for i in ITEMS if i.lang == lang}
        assert {"open_app", "close_app", "change_volume", "take_screenshot"} <= tools, lang


def test_all_apps_and_shortcuts_covered() -> None:
    apps = {i.expect_args.get("name") for i in ITEMS if i.expect_tool in ("open_app", "close_app")}
    assert not APPS - apps, f"apps never used: {APPS - apps}"
    keys = {i.expect_args.get("shortcut") for i in ITEMS if i.expect_tool == "press_shortcut"}
    assert not SHORTCUTS - keys, f"shortcuts never used: {SHORTCUTS - keys}"


def _arg_errors(item: GoldenItem) -> list[str]:
    args, tool = item.expect_args, item.expect_tool
    if tool is None:
        return [] if not args else ["Q&A items take no args"]
    errors = [f"unknown arg {k!r}" for k in args if k not in TOOL_ARGS[tool]]
    if "name" in args and args["name"] not in APPS:
        errors.append(f"app {args['name']!r}")
    if tool == "set_volume":
        level = args.get("level")
        if not (isinstance(level, int) and 0 <= level <= 100):
            errors.append(f"level {level!r}")
    if tool == "change_volume" and args.get("direction") not in VOLUME_DIRECTIONS:
        errors.append(f"direction {args.get('direction')!r}")
    if tool == "press_shortcut" and args.get("shortcut") not in SHORTCUTS:
        errors.append(f"shortcut {args.get('shortcut')!r}")
    if tool == "scroll":
        if args.get("direction") not in SCROLL_DIRECTIONS:
            errors.append(f"direction {args.get('direction')!r}")
        if "amount" in args and args["amount"] not in SCROLL_AMOUNTS:
            errors.append(f"amount {args['amount']!r}")
    for key in FREE_TEXT_KEYS & set(args):
        if not (isinstance(args[key], str) and args[key].strip()):
            errors.append(f"{key} must be non-empty text")
    return errors


def test_args_valid() -> None:
    bad = {item.id: errs for item in ITEMS if (errs := _arg_errors(item))}
    assert not bad, bad


def test_tools_needing_args_have_them() -> None:
    for item in ITEMS:
        if item.expect_tool in ("open_app", "close_app", "set_volume", "change_volume",
                                "press_shortcut", "scroll"):
            assert item.expect_args, f"{item.id}: {item.expect_tool} needs expect_args"


def test_memory_followups_present() -> None:
    followups = [i for i in ITEMS if i.history]
    assert len(followups) >= 5
    assert any(i.lang == "hinglish" and i.expect_tool == "close_app" for i in followups)
    assert all("followup" in i.tags for i in followups)


# -- scorer (pure functions, stub brain) ---------------------------------------------


@dataclass
class _Call:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Result:
    reply: str
    tool_call: _Call | None = None
    language: str = "hi-IN"


def test_match_args_rules() -> None:
    assert match_args({"level": 40}, {"level": "40"}) == []
    assert match_args({"level": 40}, {"level": 45})
    assert match_args({"name": "chrome"}, {"name": "Chrome"}) == []
    assert match_args({"query": "Arijit Singh"}, {"query": "arijit singh songs"}) == []
    assert match_args({"query": "Arijit Singh"}, {"query": "kishore kumar"})
    assert match_args({"direction": "up"}, {}) == ["missing arg 'direction'"]


def test_score() -> None:
    item = GoldenItem("x", "en", "Open Chrome", "open_app", {"name": "chrome"})
    assert score(item, _Call("open_app", {"name": "chrome"}))[0]
    assert not score(item, _Call("close_app", {"name": "chrome"}))[0]
    assert not score(item, None)[0]
    qa = GoldenItem("y", "en", "Hi", None)
    assert score(qa, None)[0]
    assert not score(qa, _Call("current_time"))[0]


class _StubBrain:
    """Opens Chrome for "kholo"/"open", closes the last app for "band"/"close it"."""

    def __init__(self) -> None:
        self.last_app: str | None = None
        self.turns: list[str] = []

    def decide(self, text: str, language: str | None) -> _Result:
        low = text.lower()
        if "band" in low or "close it" in low:
            return _Result("ok", _Call("close_app", {"name": self.last_app or "?"}))
        if "kholo" in low or "open" in low:
            self.last_app = "chrome"
            return _Result("ok", _Call("open_app", {"name": "chrome"}))
        return _Result("Namaste")

    def record_turn(self, user_text: str, reply: str, language: str) -> None:
        self.turns.append(user_text)


def test_evaluate_with_stub_brain() -> None:
    items = [
        GoldenItem("a", "hinglish", "Chrome kholo", "open_app", {"name": "chrome"}),
        GoldenItem("b", "hinglish", "Ab isko band karo", "close_app", {"name": "chrome"},
                   history=("Chrome kholo",)),
        GoldenItem("c", "en", "Who are you?", None),
        GoldenItem("d", "en", "Take a screenshot", "take_screenshot"),
    ]
    summary = evaluate(lambda: (_StubBrain(), None), items)
    assert summary["n"] == 4
    assert summary["correct"] == 3
    assert summary["per_language"]["hinglish"]["accuracy"] == 1.0
    assert summary["per_language"]["en"]["correct"] == 1
    assert [f["id"] for f in summary["failures"]] == ["d"]
    assert summary["latency_ms"]["p95"] >= 0


def test_filter_items() -> None:
    assert all(i.lang == "ta" for i in filter_items(ITEMS, langs=["ta"]))
    assert all("followup" in i.tags for i in filter_items(ITEMS, tags=["followup"]))
    assert len(filter_items(ITEMS, limit=3)) == 3

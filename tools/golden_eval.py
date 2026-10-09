"""Score the brain against the golden utterance set (PDF "Testing").

Loads ``tests/golden/utterances.yaml``, sends every utterance to a brain's
``decide(text, language)`` and checks the tool name plus a subset of arguments.
Prints accuracy per language, per expected tool and overall, mean / p95 latency of
the scored ``decide`` call, and every failure. Exits non-zero when overall accuracy
is below ``--min`` (default 0.85, the week-4 gate; 0.90 is the "done" gate).

Usage::

    python tools/golden_eval.py --fake                 # FakeLLM, no network (smoke test)
    python tools/golden_eval.py                        # real Sarvam LLM, needs SARVAM_API_KEY
    python tools/golden_eval.py --lang ta --lang te    # only some languages
    python tools/golden_eval.py --tag followup --json out.json --min 0.9

Each item gets a fresh brain with an in-memory database, so items never share
history. Items with ``history`` replay those earlier user turns first (recording
the turn and the action as the orchestrator would); only the last turn is scored.
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import re
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # allow ``python tools/golden_eval.py`` from anywhere
    sys.path.insert(0, str(ROOT))

GOLDEN_FILE = ROOT / "tests" / "golden" / "utterances.yaml"

# Language label in the YAML -> code passed to ``decide`` (what STT would report).
LANG_CODES: dict[str, str] = {
    "hi": "hi-IN",
    "hinglish": "hi-IN",
    "en": "en-IN",
    "ta": "ta-IN",
    "te": "te-IN",
    "bn": "bn-IN",
    "kn": "kn-IN",
    "mr": "mr-IN",
}

APPS = frozenset({
    "chrome", "notepad", "calculator", "paint", "file_manager", "edge", "word", "excel",
    "powerpoint", "settings", "camera",
})
SHORTCUTS = frozenset({
    "save", "copy", "paste", "undo", "redo", "select_all", "new_tab", "close_tab", "refresh",
    "enter", "minimize_all", "switch_window", "zoom_in", "zoom_out",
})
VOLUME_DIRECTIONS = frozenset({"up", "down", "mute", "unmute"})
SCROLL_DIRECTIONS = frozenset({"up", "down"})
SCROLL_AMOUNTS = frozenset({"small", "medium", "large"})

# Tool name -> argument names it accepts (the 16 golden-set actions).
TOOL_ARGS: dict[str, frozenset[str]] = {
    "open_app": frozenset({"name"}),
    "close_app": frozenset({"name"}),
    "web_search": frozenset({"query"}),
    "youtube_search": frozenset({"query"}),
    "set_volume": frozenset({"level"}),
    "change_volume": frozenset({"direction"}),
    "type_text": frozenset({"text"}),
    "press_shortcut": frozenset({"shortcut"}),
    "scroll": frozenset({"direction", "amount"}),
    "take_screenshot": frozenset(),
    "system_status": frozenset(),
    "save_note": frozenset({"title", "content"}),
    "lock_pc": frozenset(),
    "shutdown_pc": frozenset(),
    "cancel_shutdown": frozenset(),
    "current_time": frozenset(),
}

# Free-text arguments: pass when every expected word appears in the actual value.
FREE_TEXT_KEYS = frozenset({"query", "text", "title", "content"})

_WORD = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True)
class GoldenItem:
    """One golden utterance and what the brain should do with it."""

    id: str
    lang: str
    text: str
    expect_tool: str | None
    expect_args: dict[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    history: tuple[str, ...] = ()


@dataclass
class ItemResult:
    """Outcome of scoring one item."""

    id: str
    lang: str
    text: str
    ok: bool
    reason: str
    latency_ms: float
    expect_tool: str | None
    expect_args: dict[str, Any]
    got_tool: str | None = None
    got_args: dict[str, Any] = field(default_factory=dict)
    reply: str = ""


# -- loading -----------------------------------------------------------------------


def load_items(path: Path = GOLDEN_FILE) -> list[GoldenItem]:
    """Read the golden YAML file into :class:`GoldenItem` objects."""
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_items = data.get("items", []) if isinstance(data, dict) else data
    items: list[GoldenItem] = []
    for raw in raw_items:
        items.append(
            GoldenItem(
                id=str(raw["id"]),
                lang=str(raw["lang"]),
                text=str(raw["text"]),
                expect_tool=raw.get("expect_tool"),
                expect_args=dict(raw.get("expect_args") or {}),
                tags=tuple(raw.get("tags") or ()),
                history=tuple(str(h) for h in raw.get("history") or ()),
            )
        )
    return items


def filter_items(
    items: Iterable[GoldenItem],
    langs: Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
    limit: int | None = None,
) -> list[GoldenItem]:
    """Keep items in any of ``langs`` that carry any of ``tags`` (both optional)."""
    out = [
        item for item in items
        if (not langs or item.lang in langs) and (not tags or set(tags) & set(item.tags))
    ]
    return out[:limit] if limit else out


# -- scoring -----------------------------------------------------------------------


def _words(value: Any) -> list[str]:
    return [w.casefold() for w in _WORD.findall(str(value))]


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def match_args(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Return one message per expected argument the actual call gets wrong (empty = match)."""
    problems: list[str] = []
    for key, want in expected.items():
        if key not in actual:
            problems.append(f"missing arg {key!r}")
            continue
        got = actual[key]
        if isinstance(want, bool) or not isinstance(want, int | float):
            if key in FREE_TEXT_KEYS:
                missing = [w for w in _words(want) if w not in set(_words(got))]
                if missing:
                    problems.append(f"{key}={got!r} lacks {missing}")
            elif str(got).strip().casefold() != str(want).strip().casefold():
                problems.append(f"{key}={got!r}, want {want!r}")
        elif _number(got) != float(want):
            problems.append(f"{key}={got!r}, want {want!r}")
    return problems


def score(item: GoldenItem, tool_call: Any) -> tuple[bool, str]:
    """Compare a brain ``tool_call`` (or ``None``) with the item's expectation."""
    got_name = getattr(tool_call, "name", None) if tool_call is not None else None
    if item.expect_tool is None:
        return (True, "") if got_name is None else (False, f"expected no tool, got {got_name}")
    if got_name is None:
        return False, f"expected {item.expect_tool}, got no tool call"
    if got_name != item.expect_tool:
        return False, f"expected {item.expect_tool}, got {got_name}"
    args = getattr(tool_call, "args", None) or {}
    if not isinstance(args, dict):
        return False, f"args not a dict: {args!r}"
    problems = match_args(item.expect_args, args)
    return (not problems), "; ".join(problems)


# -- running -----------------------------------------------------------------------

BrainFactory = Callable[[], tuple[Any, Any]]
"""Returns a fresh ``(brain, memory)``; ``memory`` may be ``None``."""


def _replay_history(brain: Any, memory: Any, turns: Sequence[str], language: str | None) -> None:
    """Run earlier user turns and record them like the orchestrator does."""
    for text in turns:
        result = brain.decide(text, language)
        reply = getattr(result, "reply", "") or ""
        if hasattr(brain, "record_turn"):
            brain.record_turn(text, reply, getattr(result, "language", None) or language or "")
        call = getattr(result, "tool_call", None)
        if call is not None and memory is not None and hasattr(memory, "log_action"):
            memory.log_action(call.name, dict(call.args or {}), "done (golden replay)", "ok")


def run_item(factory: BrainFactory, item: GoldenItem, stt_hint: bool = True) -> ItemResult:
    """Score one item with a fresh brain. Never raises."""
    language = LANG_CODES.get(item.lang) if stt_hint else None
    base = {
        "id": item.id, "lang": item.lang, "text": item.text,
        "expect_tool": item.expect_tool, "expect_args": item.expect_args,
    }
    try:
        brain, memory = factory()
        _replay_history(brain, memory, item.history, language)
        start = time.perf_counter()
        result = brain.decide(item.text, language)
        latency = (time.perf_counter() - start) * 1000
    except Exception as exc:  # noqa: BLE001 - one bad item must not stop the run
        return ItemResult(**base, ok=False, reason=f"error: {exc!r}", latency_ms=0.0)
    call = getattr(result, "tool_call", None)
    ok, reason = score(item, call)
    return ItemResult(
        **base,
        ok=ok,
        reason=reason,
        latency_ms=latency,
        got_tool=getattr(call, "name", None) if call is not None else None,
        got_args=dict(getattr(call, "args", None) or {}) if call is not None else {},
        reply=str(getattr(result, "reply", "") or ""),
    )


def _p95(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _bucket(results: Iterable[ItemResult], key: Callable[[ItemResult], str]) -> dict[str, Any]:
    groups: dict[str, list[ItemResult]] = defaultdict(list)
    for res in results:
        groups[key(res)].append(res)
    return {
        name: {"n": len(rs), "correct": sum(r.ok for r in rs),
               "accuracy": round(sum(r.ok for r in rs) / len(rs), 4)}
        for name, rs in sorted(groups.items())
    }


def summarise(results: Sequence[ItemResult]) -> dict[str, Any]:
    """Accuracy overall / per language / per expected tool, plus latency stats."""
    latencies = [r.latency_ms for r in results if not r.reason.startswith("error")]
    correct = sum(r.ok for r in results)
    return {
        "n": len(results),
        "correct": correct,
        "accuracy": round(correct / len(results), 4) if results else 0.0,
        "per_language": _bucket(results, lambda r: r.lang),
        "per_tool": _bucket(results, lambda r: r.expect_tool or "(none)"),
        "latency_ms": {
            "mean": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
            "p95": round(_p95(latencies), 1),
        },
        "failures": [asdict(r) for r in results if not r.ok],
    }


def print_report(summary: dict[str, Any], out: Any = sys.stdout) -> None:
    """Human-readable report."""
    def line(text: str = "") -> None:
        print(text, file=out)

    line("Per language:")
    for lang, s in summary["per_language"].items():
        line(f"  {lang:<10} {s['correct']:>3}/{s['n']:<3} {s['accuracy']:.1%}")
    line("Per tool:")
    for tool, s in summary["per_tool"].items():
        line(f"  {tool:<16} {s['correct']:>3}/{s['n']:<3} {s['accuracy']:.1%}")
    line(f"Failures ({len(summary['failures'])}):")
    for f in summary["failures"]:
        line(f"  [{f['id']}] {f['text']!r} -> {f['got_tool']} {f['got_args']} :: {f['reason']}")
    lat = summary["latency_ms"]
    line(f"Latency: mean {lat['mean']} ms, p95 {lat['p95']} ms")
    line(f"Overall: {summary['correct']}/{summary['n']} = {summary['accuracy']:.1%}")


# -- brain construction (lazy: brain/actions are owned by other modules) ---------------


class BrainUnavailable(RuntimeError):
    """The brain or one of its dependencies cannot be imported or built."""



def fake_factory() -> BrainFactory:
    """``SarvamBrain`` + ``FakeLLM`` + in-memory ``Memory``. No network."""
    try:
        from brain.brain import SarvamBrain
        from brain.memory import Memory
        from core.config import load_settings

        # importlib keeps import sorting stable while brain/fake.py is still being written.
        fake_llm_cls = importlib.import_module("brain.fake").FakeLLM
    except (ImportError, AttributeError) as exc:
        raise BrainUnavailable(
            f"--fake needs brain.fake.FakeLLM and brain.brain.SarvamBrain ({exc})"
        ) from exc
    settings = load_settings(env_file=None, overrides={"DB_PATH": ":memory:"}, require_key=False)
    tools = _tool_schemas(settings, required=False)

    def make() -> tuple[Any, Any]:
        memory = Memory(":memory:")
        brain = SarvamBrain(memory, fake_llm_cls(memory), settings, tools)
        return brain, memory

    return make


def _tool_schemas(settings: Any, required: bool = True) -> list[dict[str, Any]]:
    try:
        from actions.runner import ActionRunner
    except ImportError as exc:
        if required:
            raise BrainUnavailable(f"actions.runner.ActionRunner not available ({exc})") from exc
        return []
    return list(ActionRunner(settings).tool_schemas())


def real_factory() -> BrainFactory:
    """``SarvamBrain`` + real ``SarvamLLM``. Needs ``SARVAM_API_KEY`` (.env or environment)."""
    try:
        from brain.brain import SarvamBrain
        from brain.llm_client import SarvamLLM
        from brain.memory import Memory
        from core.config import ConfigError, load_settings
        from sarvam.client import SarvamClient
    except ImportError as exc:
        raise BrainUnavailable(f"brain / sarvam modules not importable ({exc})") from exc
    try:
        settings = load_settings()
    except ConfigError as exc:
        raise BrainUnavailable(f"settings: {exc} (set SARVAM_API_KEY or use --fake)") from exc
    tools = _tool_schemas(settings)
    client = SarvamClient(settings.sarvam_api_key)
    llm = SarvamLLM.from_settings(client, settings)

    def make() -> tuple[Any, Any]:
        memory = Memory(":memory:")
        brain = SarvamBrain(memory, llm, settings, tools)
        return brain, memory

    return make


# -- CLI ---------------------------------------------------------------------------


def evaluate(
    factory: BrainFactory, items: Sequence[GoldenItem], stt_hint: bool = True
) -> dict[str, Any]:
    """Run every item and return :func:`summarise` output plus all per-item results."""
    results = [run_item(factory, item, stt_hint) for item in items]
    summary = summarise(results)
    summary["results"] = [asdict(r) for r in results]
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fake", action="store_true", help="use FakeLLM (no network)")
    parser.add_argument("--file", type=Path, default=GOLDEN_FILE, help="golden YAML file")
    parser.add_argument("--lang", action="append", help="only this language label (repeat)")
    parser.add_argument("--tag", action="append", help="only items with this tag (repeat)")
    parser.add_argument("--limit", type=int, help="score at most N items")
    parser.add_argument("--no-stt-hint", action="store_true",
                        help="pass language=None so the brain must detect the language itself")
    parser.add_argument("--min", type=float, default=0.85, help="fail below this accuracy")
    parser.add_argument("--json", type=Path, help="write the full report as JSON")
    args = parser.parse_args(argv)

    items = filter_items(load_items(args.file), args.lang, args.tag, args.limit)
    if not items:
        print("No golden items match the filters.", file=sys.stderr)
        return 2
    try:
        factory = fake_factory() if args.fake else real_factory()
    except BrainUnavailable as exc:
        print(f"Cannot build the brain: {exc}", file=sys.stderr)
        return 2

    summary = evaluate(factory, items, stt_hint=not args.no_stt_hint)
    print_report(summary)
    if args.json:
        args.json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON report: {args.json}")
    if summary["accuracy"] < args.min:
        print(f"FAIL: accuracy {summary['accuracy']:.1%} < {args.min:.0%}")
        return 1
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to a legacy code page
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())

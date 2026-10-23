"""Tests for the system prompt sections and the tool list."""

from __future__ import annotations

from brain.prompts import (
    FEW_SHOT_EXAMPLES,
    SYSTEM_PROMPT,
    build_system_prompt,
    name_for_code,
)
from brain.tools_schema import BRAIN_TOOLS, build_tools, enum_values, tool_names


def test_prompt_has_every_section() -> None:
    for heading in (
        "# Role and tone", "# Reply rules", "# Tool rules", "# Safety rules",
        "# Memory rules", "# Examples",
    ):
        assert heading in SYSTEM_PROMPT


def test_prompt_rules() -> None:
    assert "SAME language AND script" in SYSTEM_PROMPT
    assert "At most 2 short" in SYSTEM_PROMPT
    assert "exactly ONE tool" in SYSTEM_PROMPT
    assert "Never invent apps" in SYSTEM_PROMPT
    assert "Never claim an action has happened before its result" in SYSTEM_PROMPT


def test_few_shot_examples_in_many_scripts() -> None:
    for sample in ("नोटपैड खोलो", "Chrome kholo", "Set the volume", "நோட்பேட்", "వాల్యూమ్",
                   "ক্রোম", "ಸಮಯ"):
        assert sample in SYSTEM_PROMPT
    # Each example shows both a tool call and a plain answer.
    assert SYSTEM_PROMPT.count("call open_app") >= 3
    assert SYSTEM_PROMPT.count("no tool") >= 4
    # Examples speak in progress, never "done", before the result is known.
    assert "khol raha hoon" in FEW_SHOT_EXAMPLES and "diya" not in FEW_SHOT_EXAMPLES


def test_build_system_prompt() -> None:
    text = build_system_prompt("Hindi", "hi-IN", True, "Last action: open_app")
    assert text.startswith(SYSTEM_PROMPT)
    assert "speaking Hindi (hi-IN), written in Roman letters" in text
    assert text.endswith("# Context\nLast action: open_app")
    plain = build_system_prompt("Tamil", "ta-IN", False, "  ")
    assert "Roman letters, so" not in plain and "# Context" not in plain


def test_name_for_code() -> None:
    assert name_for_code("ta-IN") == "Tamil"
    assert name_for_code("xx-YY") == "xx-YY"


def test_build_tools_appends_brain_tools() -> None:
    action = {"type": "function", "function": {"name": "open_app", "parameters": {
        "type": "object", "properties": {"name": {"type": "string", "enum": ["chrome"]}}}}}
    shadow = {"type": "function", "function": {"name": "remember_fact"}}
    tools = build_tools([action, shadow])
    assert tool_names(tools) == ["open_app", "remember_fact", "clear_conversation"]
    assert tools[1] is BRAIN_TOOLS[0]
    assert build_tools(None) == BRAIN_TOOLS
    assert enum_values(tools, "open_app", "name") == ("chrome",)
    assert enum_values(tools, "open_app", "missing") is None
    assert enum_values(tools, "nope", "name") is None

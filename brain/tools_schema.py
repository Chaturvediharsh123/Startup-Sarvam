"""Tool definitions the LLM sees: the action tools plus two brain-only tools.

The action tools come from the ActionRunner (``ActionRunner.tool_schemas()``) and are
passed in by the caller; the brain never imports ``actions``. The brain-only tools
(``remember_fact``, ``clear_conversation``) touch only conversation memory, so the
brain runs them itself and they never reach the safety guard.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

REMEMBER_FACT = "remember_fact"
CLEAR_CONVERSATION = "clear_conversation"

BRAIN_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": REMEMBER_FACT,
            "description": "Save a personal fact about the user for later (name, city, ...).",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Short English key, e.g. name"},
                    "value": {"type": "string", "description": "The fact, as the user said it"},
                },
                "required": ["key", "value"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": CLEAR_CONVERSATION,
            "description": "Forget this conversation when the user asks (facts are kept).",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]

BRAIN_TOOL_NAMES: frozenset[str] = frozenset({REMEMBER_FACT, CLEAR_CONVERSATION})


def tool_name(schema: dict[str, Any]) -> str:
    """The function name of one OpenAI-style tool definition ("" if malformed)."""
    function = schema.get("function") if isinstance(schema, dict) else None
    return str(function.get("name", "")) if isinstance(function, dict) else ""


def tool_names(tools: Iterable[dict[str, Any]] | None) -> list[str]:
    """Names of every tool in ``tools``, in order."""
    return [name for name in (tool_name(t) for t in tools or ()) if name]


def enum_values(
    tools: Iterable[dict[str, Any]] | None, tool: str, param: str
) -> tuple[str, ...] | None:
    """The allowed ``enum`` values of ``tool``'s ``param``, or ``None`` if not listed."""
    for schema in tools or ():
        if tool_name(schema) != tool:
            continue
        props = schema["function"].get("parameters", {}).get("properties", {})
        values = props.get(param, {}).get("enum")
        return tuple(str(v) for v in values) if isinstance(values, list) else None
    return None


def build_tools(action_schemas: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Action tools from the ActionRunner followed by the brain-only tools.

    An action that reuses a brain-only tool name is dropped, so memory tools can
    never be shadowed by (or mistaken for) a PC action.
    """
    actions = [t for t in action_schemas or () if tool_name(t) not in BRAIN_TOOL_NAMES]
    return [*actions, *BRAIN_TOOLS]

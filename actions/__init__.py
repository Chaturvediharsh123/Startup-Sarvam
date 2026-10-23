"""WS4 Actions: do things on the PC, only through named functions in one registry.

Importing this package registers every action (``import actions`` fills
:data:`actions.registry.ACTIONS`). Use :class:`ActionRunner` to run an approved
call, or :class:`FakeActionRunner` in tests and mock mode.
"""

from __future__ import annotations

from actions import apps, notes, system, typing_keys, web  # noqa: F401 - registers actions
from actions.fake import FakeActionRunner
from actions.registry import ACTIONS, ActionSpec, tool_schemas
from actions.runner import ActionRunner

__all__ = ["ACTIONS", "ActionRunner", "ActionSpec", "FakeActionRunner", "tool_schemas"]

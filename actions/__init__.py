"""Action Block: validated, capability-based computer interaction.

Other components talk to the computer only through ``ActionExecutor`` (or
``SyncActionExecutor``). Typical use::

    executor = build_default_executor()
    result = await executor.execute({"type": "open_url", "parameters": {"url": "https://example.com"}})
"""

from actions.config import ActionSettings, AppEntry
from actions.errors import (
    ActionError,
    ActionPermissionError,
    ActionTimeoutError,
    ActionValidationError,
    AdapterError,
    PreconditionFailedError,
    TargetNotFoundError,
    UnsupportedActionError,
    VerificationError,
)
from actions.executor import ActionExecutor, SyncActionExecutor, build_default_executor
from actions.models import (
    Action,
    ActionErrorInfo,
    ActionResult,
    ActionType,
    Condition,
    ConditionCheck,
    Evidence,
    Point,
    RetryPolicy,
    Target,
)
from actions.registry import ActionRegistry, Capability

__all__ = [
    "Action", "ActionError", "ActionErrorInfo", "ActionExecutor", "ActionPermissionError", "ActionRegistry",
    "ActionResult", "ActionSettings", "ActionTimeoutError", "ActionType", "ActionValidationError", "AdapterError",
    "AppEntry", "Capability", "Condition", "ConditionCheck", "Evidence", "Point", "PreconditionFailedError",
    "RetryPolicy", "SyncActionExecutor", "Target", "TargetNotFoundError", "UnsupportedActionError",
    "VerificationError", "build_default_executor",
]

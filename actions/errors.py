"""Structured Action Block errors.

Every error knows two things the executor needs for retry decisions:

* ``retryable``: is the failure plausibly transient?
* ``may_have_side_effects``: could the computer already have been changed when it failed?
  A failure that happened *before* touching anything (target not found, validation) is safe
  to repeat even for non-idempotent actions; a timeout mid-click is not.
"""

from __future__ import annotations

from typing import Any

from actions.models import ActionErrorInfo, Evidence


class ActionError(Exception):
    error_type: str = "AdapterError"
    default_retryable: bool = False
    default_side_effects: bool = True

    def __init__(
        self,
        message: str,
        *,
        adapter: str | None = None,
        target: dict[str, Any] | None = None,
        retryable: bool | None = None,
        may_have_side_effects: bool | None = None,
        details: dict[str, Any] | None = None,
        evidence: list[Evidence] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.adapter = adapter
        self.target = target
        self.retryable = self.default_retryable if retryable is None else retryable
        self.may_have_side_effects = self.default_side_effects if may_have_side_effects is None else may_have_side_effects
        self.details = details or {}
        self.evidence = evidence or []

    def to_info(self, *, retryable: bool | None = None) -> ActionErrorInfo:
        return ActionErrorInfo(
            error_type=self.error_type,
            message=self.message,
            adapter=self.adapter,
            target=self.target,
            retryable=self.retryable if retryable is None else retryable,
            details=self.details,
        )


class UnsupportedActionError(ActionError):
    """The capability is not registered, disabled, or no adapter can run it on this machine."""

    error_type = "UnsupportedActionError"
    default_side_effects = False


class ActionValidationError(ActionError):
    error_type = "ValidationError"
    default_side_effects = False


class ActionPermissionError(ActionError):
    """Refused by a technical restriction (app/key allow-list, URL scheme, pyautogui fail-safe)."""

    error_type = "PermissionError"
    default_side_effects = False


class TargetNotFoundError(ActionError):
    error_type = "TargetNotFoundError"
    default_retryable = True
    default_side_effects = False


class PreconditionFailedError(ActionError):
    error_type = "PreconditionFailedError"
    default_retryable = True
    default_side_effects = False


class ActionTimeoutError(ActionError):
    error_type = "TimeoutError"
    default_retryable = True
    default_side_effects = True


class VerificationError(ActionError):
    """A condition the action itself waits on (``wait`` with ``until``) never held."""

    error_type = "VerificationError"
    default_retryable = True
    default_side_effects = False


class AdapterError(ActionError):
    error_type = "AdapterError"

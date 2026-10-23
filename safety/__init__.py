"""WS5 Safety: allow-list guard, spoken confirmation, emergency stop and audit log.

Safety imports only ``core`` and ``language``; it never imports ``actions`` or
``brain``. Give :class:`SafetyGuard` the tool schemas at construction.
"""

from __future__ import annotations

from safety.allowlist import Allowlist
from safety.audit_log import AuditLog
from safety.confirm import ask_yes_no, confirmation_question, is_no, is_yes
from safety.guard import ArgumentError, RateLimiter, SafetyGuard, validate_args
from safety.kill_switch import KillSwitch, enable_failsafe

__all__ = [
    "Allowlist",
    "ArgumentError",
    "AuditLog",
    "KillSwitch",
    "RateLimiter",
    "SafetyGuard",
    "ask_yes_no",
    "confirmation_question",
    "enable_failsafe",
    "is_no",
    "is_yes",
    "validate_args",
]

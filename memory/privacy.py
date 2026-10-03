"""Sensitive-data filter applied before anything reaches persistent storage.

Redacts passwords, OTPs, PINs, API keys, auth tokens, session cookies, private keys and
payment-card data (card numbers, CVV) in English, Hinglish and Hindi phrasing. It errs on
the side of over-redaction. Working memory (RAM, current task) is not filtered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_SEP = r"(?:\s*(?:is|was|hai|है|=|:|-)\s*|\s+)"

# (kind, pattern, group to redact; 0 = whole match)
_PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), 0),
    ("auth_token", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), 0),  # JWT
    ("auth_token", re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/=-]{16,})"), 1),
    ("api_key", re.compile(r"\b(?:sk|pk|rk)[-_](?:live[-_]|test[-_]|proj[-_]|ant[-_])?[A-Za-z0-9_-]{16,}"), 0),
    ("api_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), 0),
    ("api_key", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), 0),
    ("api_key", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), 0),
    ("api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), 0),
    ("api_key", re.compile(
        r"(?i)\b(?:api[\s_-]?key|secret[\s_-]?key|client[\s_-]?secret|access[\s_-]?token|auth[\s_-]?token|"
        r"refresh[\s_-]?token|secret)\b" + _SEP + r"[\"']?([A-Za-z0-9._~+/=-]{8,})"), 1),
    ("session_cookie", re.compile(r"(?i)\b(?:set-)?cookie\s*:\s*([^\n]+)"), 1),
    ("session_cookie", re.compile(r"(?i)\b(?:session[_-]?id|sessionid|phpsessid|jsessionid|sid)\s*[=:]\s*([^\s;,]+)"), 1),
    ("password", re.compile(
        r"(?i)(?:\b(?:password|passwd|passcode|pwd)\b|पासवर्ड)"
        r"(?:\s*(?:is|was|hai|है|=|:|-)\s*|\s+(?=\S*[0-9!@#$%^&*]))[\"']?(\S+)"), 1),
    ("otp", re.compile(r"(?i)(?:\b(?:otp|one[\s-]?time[\s-]?(?:password|code)|verification\s+code)\b|ओटीपी)\D{0,20}?(\d{4,8})\b"), 1),
    ("pin", re.compile(r"(?i)(?:\b(?:upi\s+|atm\s+|card\s+)?pin\b(?!\s*code)|पिन)\D{0,15}?(\d{4,6})\b"), 1),
    ("card_security_code", re.compile(r"(?i)\b(?:cvv2?|cvc2?|card\s+security\s+code)\b\D{0,10}?(\d{3,4})\b"), 1),
]

_CARD_CANDIDATE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")

_SENSITIVE_KEY_PARTS = {
    "password", "passwd", "pwd", "passcode", "otp", "pin", "cvv", "cvc", "secret", "token",
    "cookie", "cookies", "authorization", "sessionid", "apikey", "privatekey", "cardnumber",
}
_SENSITIVE_KEY_PHRASES = ("api_key", "session_id", "private_key", "card_number", "access_token",
                          "refresh_token", "client_secret", "auth_token")


def is_sensitive_key(key: str) -> bool:
    snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)  # camelCase -> camel_Case
    normalized = re.sub(r"[^a-z0-9]+", "_", snake.lower()).strip("_")
    return bool(set(normalized.split("_")) & _SENSITIVE_KEY_PARTS) or any(p in normalized for p in _SENSITIVE_KEY_PHRASES)


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def marker(kind: str) -> str:
    return f"[REDACTED:{kind}]"


@dataclass
class Redaction:
    value: Any
    kinds: set[str] = field(default_factory=set)

    @property
    def changed(self) -> bool:
        return bool(self.kinds)


class SensitiveDataFilter:
    def redact_text(self, text: str) -> Redaction:
        kinds: set[str] = set()
        for kind, pattern, group in _PATTERNS:
            def replace(m: re.Match[str], kind: str = kind, group: int = group) -> str:
                secret = m.group(group)
                if not secret or secret.startswith("[REDACTED"):
                    return m.group(0)
                kinds.add(kind)
                if group == 0:
                    return marker(kind)
                start, end = m.span(group)
                whole_start = m.start(0)
                return m.group(0)[: start - whole_start] + marker(kind) + m.group(0)[end - whole_start:]

            text = pattern.sub(replace, text)

        def replace_card(m: re.Match[str]) -> str:
            digits = re.sub(r"\D", "", m.group(0))
            if 13 <= len(digits) <= 19 and _luhn_ok(digits):
                kinds.add("card_number")
                return marker("card_number")
            return m.group(0)

        text = _CARD_CANDIDATE.sub(replace_card, text)
        return Redaction(text, kinds)

    def redact(self, value: Any) -> Redaction:
        """Recursively redact strings, and whole values under sensitive-looking dict keys."""
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            kinds: set[str] = set()
            out: dict[Any, Any] = {}
            for k, v in value.items():
                if isinstance(k, str) and is_sensitive_key(k) and v not in (None, "", [], {}) and not isinstance(v, bool):
                    out[k] = marker("sensitive_field")
                    kinds.add("sensitive_field")
                else:
                    r = self.redact(v)
                    out[k] = r.value
                    kinds |= r.kinds
            return Redaction(out, kinds)
        if isinstance(value, (list, tuple)):
            kinds = set()
            items = []
            for v in value:
                r = self.redact(v)
                items.append(r.value)
                kinds |= r.kinds
            return Redaction(type(value)(items), kinds)
        return Redaction(value)

    def contains_sensitive(self, value: Any) -> bool:
        return self.redact(value).changed

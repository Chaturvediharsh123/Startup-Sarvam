from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from memory.models import Freshness, Memory, MemorySource, MemoryStatus, MemoryType, MemoryUpdate
from memory.privacy import SensitiveDataFilter, is_sensitive_key

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
STALE = timedelta(days=90)


def mem(**kw):
    return Memory(**{"type": "fact", "content": "x", "source": "agent", **kw})


def test_confidence_defaults_by_source():
    assert mem(source="user").confidence == 0.9
    assert mem(source="agent").confidence == 0.5
    assert mem(source="execution").confidence == 0.8
    assert mem(source="agent", confidence=0.2).confidence == 0.2
    with pytest.raises(ValidationError):
        mem(confidence=1.5)


def test_categories_and_sources_are_closed_sets():
    assert {t.value for t in MemoryType} == {"fact", "preference", "episode", "observation"}
    assert {s.value for s in MemorySource} == {"user", "agent", "system", "execution"}
    with pytest.raises(ValidationError):
        mem(type="workflow")  # workflows are a separate model
    with pytest.raises(ValidationError):
        mem(source="rumour")


def test_content_validation_and_structured_content():
    with pytest.raises(ValidationError):
        mem(content="   ")
    with pytest.raises(ValidationError):
        mem(content={})
    m = mem(content={"provider": "JVVNL", "account": {"type": "domestic"}}, metadata={"tags": ["electricity"]})
    assert "provider: JVVNL" in m.text() and "type: domestic" in m.text() and "electricity" in m.text()


def test_naive_datetimes_become_utc():
    m = mem(created_at=datetime(2026, 1, 1, 10, 0))
    assert m.created_at.tzinfo == timezone.utc


def test_freshness_lifecycle():
    recent = NOW - timedelta(days=5)
    old = NOW - timedelta(days=200)
    assert mem(created_at=recent).freshness(NOW, STALE) is Freshness.UNVERIFIED
    assert mem(created_at=old).freshness(NOW, STALE) is Freshness.STALE
    assert mem(created_at=old, last_verified_at=recent).freshness(NOW, STALE) is Freshness.VERIFIED
    assert mem(created_at=old, last_verified_at=old).freshness(NOW, STALE) is Freshness.STALE
    assert mem(created_at=recent, last_verified_at=recent, expires_at=NOW - timedelta(seconds=1)).freshness(NOW, STALE) is Freshness.EXPIRED
    assert mem(status=MemoryStatus.INVALID, last_verified_at=recent).freshness(NOW, STALE) is Freshness.INVALID


def test_ids_are_safe():
    with pytest.raises(ValidationError):
        mem(id="x' OR 1=1 --")


def test_memory_update_forbids_unknown_fields():
    with pytest.raises(ValidationError):
        MemoryUpdate(source="user")  # provenance is not rewritable through update()


# --------------------------------------------------------------------------- privacy

F = SensitiveDataFilter()


@pytest.mark.parametrize(
    ("text", "kind", "secret"),
    [
        ("my password is Hunter2!", "password", "Hunter2!"),
        ("password: s3cr3t", "password", "s3cr3t"),
        ("mera password hai abc@123", "password", "abc@123"),
        ("पासवर्ड है abc@123", "password", "abc@123"),
        ("Your OTP is 482913", "otp", "482913"),
        ("otp 4829 aaya hai", "otp", "4829"),
        ("ओटीपी 482913 है", "otp", "482913"),
        ("UPI PIN 1234", "pin", "1234"),
        ("CVV 123", "card_security_code", "123"),
        ("card 4111 1111 1111 1111 exp 12/29", "card_number", "4111 1111 1111 1111"),
        ("key sk-proj-abcdefghijklmnopqrstuvwx", "api_key", "sk-proj-abcdefghijklmnopqrstuvwx"),
        ("aws AKIAABCDEFGHIJKLMNOP", "api_key", "AKIAABCDEFGHIJKLMNOP"),
        ("api_key = 9f8e7d6c5b4a3210", "api_key", "9f8e7d6c5b4a3210"),
        ("Authorization: Bearer abcdefghijklmnop1234", "auth_token", "abcdefghijklmnop1234"),
        ("token eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4", "auth_token", "eyJhbGciOiJIUzI1"),
        ("Cookie: sessionid=abc123; csrftoken=xyz", "session_cookie", "abc123"),
        ("JSESSIONID=A1B2C3D4", "session_cookie", "A1B2C3D4"),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----", "private_key", "MIIEow"),
    ],
)
def test_sensitive_text_is_redacted(text, kind, secret):
    result = F.redact_text(text)
    assert secret not in result.value, result.value
    assert kind in result.kinds


@pytest.mark.parametrize(
    "text",
    [
        "Electricity provider is JVVNL; consumer portal is energy.rajasthan.gov.in",
        "PIN code 302001",  # Indian postal code, not a secret
        "Forgot password link is at the top right",
        "Call me at 9876543210",
        "Order 1234567890123 shipped",  # 13 digits but fails Luhn
        "User prefers Hindi replies",
    ],
)
def test_ordinary_text_is_untouched(text):
    result = F.redact_text(text)
    assert result.value == text and not result.kinds


def test_sensitive_dict_keys_are_redacted_recursively():
    data = {"login": {"username": "ravi", "password": "p@ss", "otp": 1234}, "footprint": "big",
            "steps": [{"apiKey": "zzz"}], "sensitive": True}
    result = F.redact(data)
    assert result.value["login"]["username"] == "ravi"
    assert "p@ss" not in str(result.value) and "1234" not in str(result.value) and "zzz" not in str(result.value)
    assert result.value["footprint"] == "big" and result.value["sensitive"] is True


def test_sensitive_key_names():
    for key in ("password", "userPassword", "otp_code", "upi_pin", "API-Key", "session_id", "cardNumber", "cvv"):
        assert is_sensitive_key(key), key
    for key in ("footprint", "keys", "key", "pincode", "spinner", "description", "tokenizer_name"):
        assert not is_sensitive_key(key), key

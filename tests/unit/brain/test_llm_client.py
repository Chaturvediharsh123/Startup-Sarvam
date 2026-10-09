"""Tests for the LLM request deadline."""

from __future__ import annotations

import time
from typing import Any

import pytest

from brain.llm_client import TIMEOUT_CODE, SarvamLLM, call_with_timeout
from core.config import Settings
from sarvam.client import SarvamError


def test_call_with_timeout_returns_value() -> None:
    assert call_with_timeout(lambda: 42, 1.0) == 42
    assert call_with_timeout(lambda: 7, None) == 7


def test_call_with_timeout_reraises_errors() -> None:
    def boom() -> None:
        raise SarvamError("HTTP 500", 500)

    with pytest.raises(SarvamError, match="HTTP 500"):
        call_with_timeout(boom, 1.0)


def test_call_with_timeout_raises_on_deadline() -> None:
    start = time.perf_counter()
    with pytest.raises(SarvamError) as info:
        call_with_timeout(lambda: time.sleep(2), 0.1)
    assert info.value.code == TIMEOUT_CODE
    assert time.perf_counter() - start < 1


class Client:
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.payloads: list[dict[str, Any]] = []

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        time.sleep(self.delay)
        return {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}


def test_sarvam_llm_times_out() -> None:
    llm = SarvamLLM(Client(2), "sarvam-105b", timeout_s=0.1)  # type: ignore[arg-type]
    with pytest.raises(SarvamError, match="did not answer"):
        llm.chat([{"role": "user", "content": "x"}])


def test_sarvam_llm_from_settings(settings: Settings) -> None:
    client = Client(0)
    llm = SarvamLLM.from_settings(client, settings)  # type: ignore[arg-type]
    assert llm.timeout_s == settings.llm_timeout_s
    assert llm.model == settings.llm_model and llm.temperature == 0.2
    assert llm.chat([{"role": "user", "content": "x"}])["content"] == "hi"
    assert client.payloads[0]["temperature"] == 0.2

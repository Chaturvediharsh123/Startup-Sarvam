"""Tests for the Sarvam HTTP client and LLM wrapper using httpx.MockTransport."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from sarvam.client import SarvamClient, SarvamError
from sarvam.llm import CHAT_PATH, SarvamLLM

Handler = Callable[[httpx.Request], httpx.Response]


def make_client(handler: Handler, sleeps: list[float] | None = None) -> SarvamClient:
    record = sleeps if sleeps is not None else []
    return SarvamClient(
        "sk_test", transport=httpx.MockTransport(handler), sleep=record.append
    )


def test_auth_header_and_json() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    client = make_client(handler)
    assert client.post_json("/x", {"a": 1}) == {"ok": True}
    assert seen[0].headers["api-subscription-key"] == "sk_test"
    assert str(seen[0].url) == "https://api.sarvam.ai/x"
    assert json.loads(seen[0].content) == {"a": 1}


def test_retries_on_429_then_succeeds() -> None:
    responses = [
        httpx.Response(429, json={"error": {"message": "slow down"}}),
        httpx.Response(503, json={"error": {"message": "busy"}}),
        httpx.Response(200, json={"ok": 1}),
    ]
    sleeps: list[float] = []
    client = make_client(lambda r: responses.pop(0), sleeps)
    assert client.post_json("/x", {}) == {"ok": 1}
    assert sleeps == [0.5, 1.0]


def test_gives_up_after_retries() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        body = {"error": {"message": "boom", "code": "internal_server_error"}}
        return httpx.Response(500, json=body)

    with pytest.raises(SarvamError) as info:
        make_client(handler).post_json("/x", {})
    assert len(calls) == 3
    assert info.value.status == 500
    assert info.value.code == "internal_server_error"


@pytest.mark.parametrize("status", [400, 403, 422])
def test_no_retry_on_client_errors(status: int) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status, json={"error": {"message": "bad", "request_id": "r1"}})

    with pytest.raises(SarvamError) as info:
        make_client(handler).post_json("/x", {})
    assert len(calls) == 1
    assert info.value.request_id == "r1"
    if status == 403:
        assert "SARVAM_API_KEY" in str(info.value)


def test_retry_after_header_respected() -> None:
    responses = [httpx.Response(429, headers={"retry-after": "2"}), httpx.Response(200, json={})]
    sleeps: list[float] = []
    make_client(lambda r: responses.pop(0), sleeps).post_json("/x", {})
    assert sleeps == [2.0]


def test_network_error_wrapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no internet")

    with pytest.raises(SarvamError, match="Network error"):
        make_client(handler).post_json("/x", {})


def test_stream_post_yields_bytes() -> None:
    client = make_client(lambda r: httpx.Response(200, content=b"abcdef"))
    with client.stream_post("/s", {}) as response:
        assert b"".join(response.iter_bytes()) == b"abcdef"


def test_ws_url() -> None:
    url = SarvamClient.ws_url("/speech-to-text-realtime/ws", {"language_code": "auto", "x": None})
    assert url == "wss://api.sarvam.ai/speech-to-text-realtime/ws?language_code=auto"


def test_llm_payload_and_message() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == CHAT_PATH
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "tool_calls", "index": 0, "message": {
                "role": "assistant", "content": None, "reasoning_content": "thinking...",
                "tool_calls": [{"id": "call_1", "type": "function", "function": {
                    "name": "open_app", "arguments": "{\"name\":\"notepad\"}"}}],
            }}],
            "usage": {"total_tokens": 10},
        })

    llm = SarvamLLM(make_client(handler), "sarvam-105b", reasoning="low")
    tools = [{"type": "function", "function": {"name": "open_app", "parameters": {}}}]
    message = llm.chat([{"role": "user", "content": "Notepad kholo"}], tools)
    assert message["tool_calls"][0]["function"]["name"] == "open_app"
    assert "reasoning_content" not in message
    body = seen[0]
    assert body["model"] == "sarvam-105b"
    assert body["tools"] == tools and body["tool_choice"] == "auto"
    assert body["reasoning_effort"] == "low"


def test_llm_reasoning_none_sends_null() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        message = {"role": "assistant", "content": "hi"}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    SarvamLLM(make_client(handler), "sarvam-105b", reasoning="none").chat([], None)
    assert seen[0]["reasoning_effort"] is None
    assert "tools" not in seen[0]


def test_llm_malformed_response() -> None:
    llm = SarvamLLM(make_client(lambda r: httpx.Response(200, json={"oops": 1})), "sarvam-105b")
    with pytest.raises(SarvamError):
        llm.chat([], None)

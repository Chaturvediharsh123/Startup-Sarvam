"""Tests for the core contracts: events, the event bus, the state machine, logging."""

from __future__ import annotations

import logging
import queue
import threading
import time

import pytest

from core.bus import EventBus
from core.events import (
    ActionResult,
    Event,
    FinalTranscript,
    Interrupt,
    PartialTranscript,
    StateChanged,
    Status,
    TurnState,
    new_turn_id,
)
from core.interfaces import CancelToken
from core.logging import TurnContextFilter, current_turn, mask_secrets
from core.state import IllegalTransition, StateMachine


def _wait_until(check, timeout: float = 2.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.01)
    return False


# -- events -------------------------------------------------------------------------


def test_every_event_has_turn_id_and_timestamp() -> None:
    event = FinalTranscript("Notepad kholo", "hi-IN", 0.9, turn_id="t1")
    assert event.turn_id == "t1"
    assert event.ts > 0
    assert event.kind == "final_transcript"
    assert isinstance(event, Event)


def test_action_result_keeps_positional_fields() -> None:
    result = ActionResult("open_app", {"name": "chrome"}, "ok", "Opened chrome", 12, fact="opened")
    assert result.ok and result.duration_ms == 12 and result.fact == "opened"
    assert not ActionResult("x", {}, "timeout", None).ok


def test_turn_ids_are_unique() -> None:
    ids = {new_turn_id() for _ in range(100)}
    assert len(ids) == 100


# -- bus ----------------------------------------------------------------------------


def test_bus_delivers_only_subscribed_types() -> None:
    bus = EventBus()
    got: list[Event] = []
    bus.subscribe(FinalTranscript, got.append, name="test")
    bus.publish(PartialTranscript("no"))
    bus.publish(FinalTranscript("yes"))
    assert _wait_until(lambda: len(got) == 1)
    assert isinstance(got[0], FinalTranscript)
    bus.close()


def test_bus_handler_errors_do_not_crash_the_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.bus

    logged: list[str] = []
    monkeypatch.setattr(core.bus.logger, "exception", lambda msg, *a: logged.append(msg % a))
    bus = EventBus()
    got: list[Event] = []

    def bad(event: Event) -> None:
        raise ValueError("boom")

    bus.subscribe(Status, bad)
    bus.subscribe(Status, got.append)
    bus.publish(Status("one"))
    bus.publish(Status("two"))
    assert _wait_until(lambda: len(got) == 2 and len(logged) == 2)
    assert "Bus handler failed for Status" in logged[0]
    bus.close()


def test_publish_does_not_wait_for_slow_subscribers() -> None:
    bus = EventBus()
    release = threading.Event()
    bus.subscribe(Status, lambda e: release.wait(2))
    start = time.perf_counter()
    for i in range(50):
        bus.publish(Status(str(i)))
    assert time.perf_counter() - start < 0.5
    release.set()
    bus.close()


def test_publish_from_many_threads_and_polling_queue() -> None:
    bus = EventBus()
    inbox = bus.queue((Interrupt,))
    threads = [threading.Thread(target=bus.publish, args=(Interrupt("t"),)) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    received = 0
    while True:
        try:
            inbox.get_nowait()
            received += 1
        except queue.Empty:
            break
    assert received == 20
    bus.close()


def test_unsubscribe_and_close_are_safe() -> None:
    bus = EventBus()
    got: list[Event] = []
    unsubscribe = bus.subscribe((), got.append)
    bus.publish(Status("a"))
    assert _wait_until(lambda: len(got) == 1)
    unsubscribe()
    bus.publish(Status("b"))
    time.sleep(0.05)
    assert len(got) == 1
    bus.close()
    bus.close()
    bus.publish(Status("after close"))  # ignored, no error


# -- state machine ------------------------------------------------------------------


def test_state_machine_follows_pdf_table() -> None:
    published: list[StateChanged] = []
    sm = StateMachine(published.append, strict=True)
    for state in (TurnState.LISTENING, TurnState.THINKING, TurnState.CHECKING,
                  TurnState.AWAITING_YES, TurnState.ACTING, TurnState.SPEAKING,
                  TurnState.LISTENING):
        sm.to(state, "t1")
    assert sm.state is TurnState.LISTENING
    assert [p.new for p in published][-1] is TurnState.LISTENING
    assert all(p.turn_id == "t1" for p in published)


def test_illegal_transition_raises_in_strict_mode() -> None:
    sm = StateMachine(strict=True, initial=TurnState.THINKING)
    with pytest.raises(IllegalTransition):
        sm.to(TurnState.ACTING)


def test_interrupt_can_reset_from_any_state() -> None:
    for state in TurnState:
        sm = StateMachine(strict=True, initial=state)
        sm.reset(live=True)
        assert sm.state is TurnState.LISTENING


def test_cancel_token() -> None:
    token = CancelToken()
    assert not token.cancelled and not token.wait(0.01)
    token.cancel("voice")
    assert token.cancelled and token.reason == "voice" and token.wait(0.01)


# -- logging ------------------------------------------------------------------------


def test_secrets_are_masked() -> None:
    text = mask_secrets("key sk_abcdef1234567890xyz and api-subscription-key: sk_zzzzzz999")
    assert "1234567890" not in text and "999" not in text
    assert "***" in text


def test_turn_id_is_added_to_records() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello %s", ("sk_secretkey123456",),
                               None)
    token = current_turn.set("t0042-1")
    try:
        TurnContextFilter().filter(record)
    finally:
        current_turn.reset(token)
    assert record.turn_id == "t0042-1"
    assert "secretkey123456" not in record.getMessage()

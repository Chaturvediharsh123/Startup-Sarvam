"""Hidden-window smoke tests: a fake orchestrator, a real EventBus, events in, widgets out.

Skipped cleanly when Tk has no display.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import Any

import pytest

from core.bus import EventBus
from core.config import Settings
from core.events import (
    ActionResult,
    FinalTranscript,
    HealthChanged,
    LanguageDetected,
    Metrics,
    MicLevel,
    PartialTranscript,
    SafetyDecision,
    SpeakRequest,
    StateChanged,
    ToolCall,
    TurnState,
    Verdict,
)
from core.interfaces import Health

ctk = pytest.importorskip("customtkinter")
tkinter = pytest.importorskip("tkinter")

TK_ATTEMPTS = 5


class FakeOrchestrator:
    """Records every call the window makes."""

    def __init__(self, live: bool = False, start_ok: bool = True) -> None:
        self._live = live
        self.start_ok = start_ok
        self.calls: list[tuple[str, Any]] = []

    @property
    def live(self) -> bool:
        return self._live

    def start_live(self) -> bool:
        self.calls.append(("start_live", None))
        self._live = self.start_ok
        return self.start_ok

    def stop_live(self) -> None:
        self.calls.append(("stop_live", None))
        self._live = False

    def ptt_press(self) -> None:
        self.calls.append(("ptt_press", None))

    def ptt_release(self) -> None:
        self.calls.append(("ptt_release", None))

    def submit_typed(self, text: str) -> None:
        self.calls.append(("submit_typed", text))

    def interrupt(self, reason: str = "user") -> None:
        self.calls.append(("interrupt", reason))

    def set_language_hint(self, code: str | None) -> None:
        self.calls.append(("set_language_hint", code))

    def set_wake_word(self, word: str) -> None:
        self.calls.append(("set_wake_word", word))

    def health(self) -> list[Health]:
        return [Health("audio", True), Health("stt", False, "offline")]

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


class FakeAudit:
    def __init__(self) -> None:
        self.rows = [{"time": "2026-10-08T10:00:00", "turn_id": "t1", "kind": "decision",
                      "tool": "delete_file", "verdict_or_status": "deny",
                      "reason_or_fact": "outside the notes folder"}]

    def recent(self, n: int) -> list[dict[str, Any]]:
        return self.rows[-n:]


@pytest.fixture
def bus() -> Iterator[EventBus]:
    bus = EventBus()
    yield bus
    bus.close()


@pytest.fixture
def make_app(bus: EventBus, settings: Settings) -> Iterator[Callable[..., Any]]:
    apps: list[Any] = []

    def make(orchestrator: FakeOrchestrator, **kwargs: Any) -> Any:
        from ui.app import AssistantApp

        chosen = kwargs.pop("settings", settings)
        error: Exception | None = None
        # On Windows, Tcl start-up sometimes fails while pytest captures stdout; retry.
        for _ in range(TK_ATTEMPTS):
            try:
                app = AssistantApp(orchestrator, bus, chosen, **kwargs)
                break
            except tkinter.TclError as exc:
                error = exc
        else:
            pytest.skip(f"no display: {error}")
        app.withdraw()
        apps.append(app)
        return app

    yield make
    for app in apps:
        app.on_close()


def pump(app: Any, until: Callable[[], bool], timeout: float = 3.0) -> None:
    """Run the Tk loop until ``until()`` is true (the 50 ms poll must fire)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.update()
        if until():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met while pumping the Tk loop")


def test_window_shows_bus_events(make_app: Callable[..., Any], bus: EventBus) -> None:
    orch = FakeOrchestrator()
    app = make_app(orch, audit=FakeAudit())
    assert app.status_bar.dots["stt"].cget("text").endswith("✗")  # seeded from health()
    assert "outside the notes folder" in app.audit_view.text()  # seeded from the audit log
    for event in (
        StateChanged(TurnState.IDLE, TurnState.LISTENING),
        PartialTranscript("नोटपैड"),
        FinalTranscript("नोटपैड खोलो", "hi-IN"),
        PartialTranscript("आवाज़"),
        LanguageDetected("hi-IN"),
        SpeakRequest("नोटपैड खोल दिया।"),
        ActionResult("open_app", {"name": "notepad"}, "ok", "Opened", fact="opened notepad"),
        SafetyDecision(Verdict.DENY, ToolCall("shutdown_pc"), reason="not in allow-list"),
        Metrics(stt_ms=400, llm_ms=900, tts_first_ms=300, total_ms=1600),
        HealthChanged("tts", True),
        MicLevel(0.5),
        StateChanged(TurnState.CHECKING, TurnState.AWAITING_YES),
    ):
        bus.publish(event)
    pump(app, lambda: app.status_bar.state_label.cget("text") == "Waiting for your yes")
    transcript = app.transcript.text()
    assert "नोटपैड खोलो" in transcript
    assert transcript.count("नोटपैड") == 1  # the partial was replaced, not duplicated
    assert transcript.endswith("आवाज़")  # the new partial is shown after the final text
    assert app.transcript.box.tag_ranges("partial")
    assert app.reply.said.cget("text") == "नोटपैड खोल दिया।"
    assert "not in allow-list" in app.reply.did.cget("text")  # the latest outcome wins
    assert "open_app" in app.activity.text()
    assert "not in allow-list" in app.audit_view.text()
    assert app.speed.values["total_ms"].cget("text") == "1600 ms"
    assert app.status_bar.language_label.cget("text") == "Language: hi-IN"
    assert app.status_bar.detail_label.cget("text") == "Say or type yes / no"
    assert app.status_bar.dots["tts"].cget("text_color") == app.look.palette.ok
    assert app.status_bar.level.get() == pytest.approx(0.5)


def test_controls_call_the_orchestrator(make_app: Callable[..., Any]) -> None:
    orch = FakeOrchestrator(live=True)
    app = make_app(orch)
    assert app.live_switch.get() == 1  # reflects orchestrator.live at start
    assert app.bind_all("<F9>") and app.bind("<Escape>") and app.bind("<Control-l>")
    assert app.bind("<KeyPress-F8>") and app.bind("<KeyRelease-F8>")

    app.entry.insert(0, "Notepad kholo")
    app._send_typed()
    assert ("submit_typed", "Notepad kholo") in orch.calls
    assert app.entry.get() == ""

    app.stop()  # Stop button / F9 / Esc
    assert orch.calls[-1] == ("interrupt", "user")

    app.toggle_live()  # Ctrl+L: live -> off, on the background worker
    pump(app, lambda: "stop_live" in orch.names())
    pump(app, lambda: app.live_switch.get() == 0)

    app.controls.press_ptt()
    app.controls.press_ptt()  # key auto-repeat: ignored
    app.controls.release_ptt()
    pump(app, lambda: orch.names().count("ptt_release") == 1)
    assert orch.names().count("ptt_press") == 1

    app.controls._on_language("தமிழ் · ta-IN")
    pump(app, lambda: ("set_language_hint", "ta-IN") in orch.calls)
    app.controls.wake_entry.insert(0, "sarvam")
    app.controls._on_wake_entry()
    assert orch.calls[-1] == ("set_wake_word", "sarvam")
    assert app.controls.wake_switch.get() == 1
    app.controls.wake_switch.toggle()  # switch off -> wake word disabled
    assert orch.calls[-1] == ("set_wake_word", "")


def test_live_mode_failure_is_shown(make_app: Callable[..., Any]) -> None:
    orch = FakeOrchestrator(live=False, start_ok=False)
    app = make_app(orch)
    app.toggle_live()
    pump(app, lambda: "start_live" in orch.names() and app.live_switch.get() == 0)
    pump(app, lambda: "live mode" in app.status_bar.detail_label.cget("text"))
    assert app.status_bar.detail_label.cget("text_color") == app.look.palette.bad


def test_settings_change_theme_font_and_language_live(
    make_app: Callable[..., Any], bus: EventBus, settings: Settings
) -> None:
    saved: list[dict[str, str]] = []
    orch = FakeOrchestrator()
    app = make_app(orch, save_settings=saved.append)
    bus.publish(FinalTranscript("hello there"))
    pump(app, lambda: "hello there" in app.transcript.text())

    from ui.app import SettingsDialog

    dialog = SettingsDialog(app, app.look, settings, app.apply_settings,
                            devices=lambda kind: ["Mic A"] if kind == "input" else None)
    values = dialog.values()
    assert set(values) == {"MIC_DEVICE", "SPEAKER_DEVICE", "TTS_SPEAKER", "UI_FONT_SIZE",
                           "UI_THEME", "UI_LANGUAGE", "WAKE_WORD", "LANGUAGE_HINT"}
    assert values["MIC_DEVICE"] == ""  # "System default"
    dialog.theme.set("High contrast")
    dialog.ui_language.set("हिन्दी")
    dialog.font_size.set("24")
    dialog.wake.insert(0, "sarvam")
    dialog.hint.set("English · en-IN")
    dialog.mic.set("Mic A")
    dialog.save()

    assert saved[-1]["UI_THEME"] == "high_contrast"
    assert saved[-1]["MIC_DEVICE"] == "Mic A"
    assert app.look.palette.name == "high_contrast"
    assert app.look.sizes.base == 24
    assert app.title() == "सर्वम सहायक"
    assert app.controls.stop.cget("text") == "रोकें (F9)"
    assert "hello there" in app.transcript.text()  # content survives the rebuild
    assert ("set_wake_word", "sarvam") in orch.calls
    pump(app, lambda: ("set_language_hint", "en-IN") in orch.calls)
    assert app.status_bar.detail_label.cget("text").startswith("सेटिंग्स सहेजी गईं")


def test_failed_save_is_shown_not_raised(make_app: Callable[..., Any],
                                         settings: Settings) -> None:
    def broken(_values: dict[str, str]) -> None:
        raise OSError("disk full")

    app = make_app(FakeOrchestrator(), settings=replace(settings, ui_theme="light"),
                   save_settings=broken)
    app.apply_settings({"UI_THEME": "light", "WAKE_WORD": ""})
    assert "disk full" in app.status_bar.detail_label.cget("text")

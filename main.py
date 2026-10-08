"""Entry point for the Sarvam voice PC assistant: builds the bus, wires modules, starts the UI.

Usage::

    python main.py            # window + live listening
    python main.py --ptt      # window + push-to-talk instead of live listening
    python main.py --no-ui    # terminal only, live listening
    python main.py --text     # type commands instead of speaking (terminal)
    python main.py --mock     # no Sarvam key needed: keyword "LLM", typed input, window
    python main.py --mock --text   # same, in the terminal

``main.py`` is the only place that knows the concrete modules. Everything else talks
through ``core.events`` on the :class:`~core.bus.EventBus` and ``core.interfaces``.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from actions.runner import ActionRunner
from audio.fake import FakeSpeaker
from audio.mic import Microphone, MuteFlag, parse_device
from audio.speaker import AudioPlayer, stream_for_device
from brain.brain import SarvamBrain
from brain.fake import FakeLLM
from brain.llm_client import ChatModel, SarvamLLM
from brain.memory import Memory
from core.bus import EventBus
from core.config import ConfigError, Settings, get_settings, load_settings, save_env_values
from core.events import (
    ActionResult,
    BrainResult,
    ErrorRaised,
    Event,
    FinalTranscript,
    HealthChanged,
    Interrupt,
    Metrics,
    PartialTranscript,
    SafetyDecision,
    SpeakRequest,
    Status,
)
from core.logging import setup_logging, shutdown_logging
from core.orchestrator import Orchestrator
from safety.audit_log import AuditLog
from safety.guard import SafetyGuard
from safety.kill_switch import KillSwitch
from sarvam.client import SarvamClient
from stt.batch_client import RestSTT
from stt.realtime_client import RealtimeSTT
from stt.wake_word import strip_wake_word
from tts.fake import MockTTS, SilentTTS
from tts.stream_client import SarvamTTS

logger = logging.getLogger(__name__)

STATUS_MARKS = {"ok": "✓", "error": "✗", "denied": "denied", "blocked": "blocked",
                "timeout": "timeout", "cancelled": "cancelled"}


@dataclass
class App:
    """Every module of one running assistant, built by :func:`build_app`."""

    settings: Settings
    bus: EventBus
    client: SarvamClient
    memory: Memory
    runner: ActionRunner
    guard: SafetyGuard
    brain: SarvamBrain
    orchestrator: Orchestrator
    audit: AuditLog
    kill_switch: KillSwitch | None = None
    _closers: list[Callable[[], None]] = field(default_factory=list)

    def close(self) -> None:
        """Stop every module in reverse start order. Safe to call twice."""
        closers, self._closers = self._closers, []
        for close in reversed(closers):
            try:
                close()
            except Exception:  # noqa: BLE001 - shutdown must finish
                logger.exception("Error while shutting down")


class _NoLiveSTT:
    """Mock mode has no speech recognition; live mode reports that and stops."""

    language_hint = ""

    def __init__(self, on_event: Callable[[str], None]) -> None:
        self._on_event = on_event

    def run_blocking(self, read_audio: Callable[[float], bytes | None],
                     stop: threading.Event) -> None:
        """Report that live listening is unavailable and return."""
        self._on_event("error:Live listening needs a Sarvam API key (mock mode)")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line flags."""
    parser = argparse.ArgumentParser(
        description="Voice-controlled Windows PC assistant (Sarvam AI)"
    )
    parser.add_argument("--text", action="store_true", help="type commands instead of speaking")
    parser.add_argument("--ptt", action="store_true", help="push-to-talk instead of live listening")
    parser.add_argument("--no-ui", action="store_true", help="run in the terminal without a window")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    parser.add_argument(
        "--mock", action="store_true",
        help="offline test mode: keyword rules instead of Sarvam, typed input, no API key",
    )
    parser.add_argument("--no-kill-switch", action="store_true",
                        help="turn off the mouse-in-corner emergency stop (tests only)")
    return parser.parse_args(argv)


def build_app(
    settings: Settings,
    *,
    mock: bool = False,
    text: bool = False,
    kill_switch: bool = True,
    bus: EventBus | None = None,
) -> App:
    """Create every module and wire them through the bus and the orchestrator.

    Args:
        mock: keyword-rule LLM and offline voice; nothing calls Sarvam.
        text: terminal typing mode; replies are printed, no sound device is opened.
        kill_switch: watch for the mouse in a screen corner (emergency stop).
    """
    bus = bus or EventBus()
    closers: list[Callable[[], None]] = [bus.close]
    client = SarvamClient(settings.sarvam_api_key)
    closers.append(client.close)
    memory = Memory(settings.db_path, settings.default_language)
    closers.append(memory.close)

    runner = ActionRunner(settings)
    runner.start()
    closers.append(runner.stop)
    guard = SafetyGuard(settings, schemas_provider=runner.tool_schemas)
    guard.start()
    closers.append(guard.stop)

    llm: ChatModel = FakeLLM(memory) if mock else SarvamLLM.from_settings(client, settings)
    brain = SarvamBrain(memory, llm, settings, runner.tool_schemas())
    brain.start()
    closers.append(brain.stop)

    audit = AuditLog.from_settings(settings)
    bus.subscribe((BrainResult, SafetyDecision, ActionResult), audit.handle, name="audit")

    mute = MuteFlag()
    holder: dict[str, Orchestrator] = {}

    def on_level(level: float) -> None:
        if "o" in holder:
            holder["o"].on_mic_level(level)

    def on_mic_error(message: str) -> None:
        bus.publish(ErrorRaised(message, "audio"))
        bus.publish(HealthChanged("audio", False, message))

    def on_mic_recovered() -> None:
        bus.publish(HealthChanged("audio", True, "microphone back"))
        bus.publish(Status("microphone back"))

    mic = Microphone(
        settings.sample_rate, settings.chunk_ms, mute, parse_device(settings.mic_device),
        on_level=on_level, noise_gate=settings.noise_gate, on_error=on_mic_error,
        on_recovered=on_mic_recovered,
    )
    speaker_stream = stream_for_device(parse_device(settings.speaker_device))
    tts: Any
    player: AudioPlayer
    if text:
        tts = MockTTS(settings, output=lambda _text: None) if mock else SilentTTS(
            settings.tts_sample_rate)
        player = FakeSpeaker(settings.tts_sample_rate, mute)
    elif mock:
        tts = MockTTS(settings, output=lambda _text: None)
        player = AudioPlayer(settings.tts_sample_rate, mute, stream_factory=speaker_stream)
    else:
        tts = SarvamTTS(client, settings)
        # With barge-in on, the mic stays open while speaking (use headphones).
        player = AudioPlayer(settings.tts_sample_rate, None if settings.barge_in else mute,
                             stream_factory=speaker_stream)

    def stt_factory(on_partial, on_final, on_event):  # type: ignore[no-untyped-def]
        """A live STT client wired to the orchestrator callbacks."""
        if mock:
            return _NoLiveSTT(on_event)
        return RealtimeSTT(client, settings, on_partial, on_final, on_event)

    switch: KillSwitch | None = None

    def lift_emergency_stop() -> None:
        """A fresh user command after an emergency stop re-enables actions."""
        if getattr(guard, "halted", False) or getattr(runner, "halted", False):
            guard.resume()
            runner.resume()
            logger.info("Emergency stop lifted by a new command")

    orchestrator = Orchestrator(
        settings, bus, brain, guard, runner, tts, player, mic, stt_factory,
        batch_stt=None if mock else RestSTT(client, settings),
        wake_filter=strip_wake_word,
        modules={"audio": mic, "brain": brain, "actions": runner, "safety": guard,
                 "tts": tts},
        before_turn=lift_emergency_stop,
    )
    holder["o"] = orchestrator
    closers.append(orchestrator.shutdown)

    if kill_switch and not text:
        def emergency_stop() -> None:
            guard.halt("emergency stop")
            runner.halt("emergency stop")
            orchestrator.interrupt("emergency stop")

        switch = KillSwitch(emergency_stop)
        try:
            switch.start()
            closers.append(switch.stop)
        except Exception:  # noqa: BLE001 - no screen access (e.g. CI); keep running
            logger.warning("Emergency-stop corner unavailable", exc_info=True)
            switch = None

    return App(settings, bus, client, memory, runner, guard, brain, orchestrator, audit,
               switch, closers)


# -- terminal output ----------------------------------------------------------------


def format_metrics(m: Metrics) -> str:
    """One line of timings, e.g. ``STT 0.4s · LLM 0.8s · first audio 0.3s · total 1.6s``."""
    return (f"STT {m.stt_ms / 1000:.1f}s · LLM {m.llm_ms / 1000:.1f}s · action "
            f"{m.action_ms / 1000:.1f}s · first audio {m.tts_first_ms / 1000:.1f}s · "
            f"total {m.total_ms / 1000:.1f}s")


def print_event(event: Event, echo_user: bool = True) -> None:
    """Show one bus event in the terminal (``echo_user=False`` when the user typed it)."""
    if isinstance(event, PartialTranscript):
        print(f"\r  … {event.text}", end="", flush=True)
    elif isinstance(event, FinalTranscript):
        if echo_user:
            print(f"\rYou: {event.text}")
    elif isinstance(event, SpeakRequest):
        print(f"Assistant: {event.text}")
    elif isinstance(event, SafetyDecision):
        if event.verdict.value != "allow":
            print(f"  [safety] {event.verdict.value}: {event.tool_call.name} {event.reason}")
    elif isinstance(event, ActionResult):
        mark = STATUS_MARKS.get(event.status, event.status)
        print(f"  [{mark}] {event.name}({event.args}) -> {event.fact or event.result}")
    elif isinstance(event, Metrics):
        print(f"  ({format_metrics(event)})")
    elif isinstance(event, ErrorRaised):
        print(f"  [error] {event.message}")
    elif isinstance(event, Interrupt):
        print(f"  [stopped: {event.reason}]")
    elif isinstance(event, Status) and event.text.startswith(("busy", "no answer")):
        print(f"  [{event.text}]")


def _wait_for_turn(orchestrator: Orchestrator, timeout: float = 60.0) -> None:
    """Block until the turn finished or a yes/no answer is needed."""
    time.sleep(0.05)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if orchestrator.awaiting_answer or not orchestrator.busy:
            break
        time.sleep(0.02)
    time.sleep(0.15)  # let the printer thread catch up


def run_text_mode(app: App) -> None:
    """Type a command, see the reply. Risky actions ask for a typed yes."""
    app.bus.subscribe((), lambda e: print_event(e, echo_user=False), name="terminal")
    orchestrator = app.orchestrator
    print("Text mode. Type a command in any language. 'exit' to quit.")
    while True:
        prompt = "You (haan/nahi): " if orchestrator.awaiting_answer else "You: "
        try:
            line = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            if orchestrator.awaiting_answer:
                orchestrator.submit_typed("nahi")
                _wait_for_turn(orchestrator)
            break
        if line.lower() in ("exit", "quit", "bye"):
            break
        if line:
            orchestrator.submit_typed(line)
            _wait_for_turn(orchestrator)


def register_hotkeys(orchestrator: Orchestrator, push_to_talk: bool) -> Callable[[], None]:
    """Global F9 = stop; F8 held = push-to-talk. Returns a cleanup function."""
    try:
        import keyboard

        keyboard.add_hotkey("f9", orchestrator.interrupt)
        if push_to_talk:
            keyboard.on_press_key("f8", lambda _e: orchestrator.ptt_press())
            keyboard.on_release_key("f8", lambda _e: orchestrator.ptt_release())
    except Exception:  # noqa: BLE001 - hotkeys are a convenience, never fatal
        logger.warning("Global hotkeys unavailable", exc_info=True)
        return lambda: None

    def cleanup() -> None:
        """Remove every global hotkey this app registered."""
        try:
            keyboard.unhook_all()
        except Exception:  # noqa: BLE001 - cleanup must not raise at exit
            logger.debug("Could not remove hotkeys", exc_info=True)

    return cleanup


def run_terminal_voice(app: App, push_to_talk: bool) -> None:
    """Voice mode without a window. Ctrl+C to quit."""
    app.bus.subscribe((), print_event, name="terminal")
    if push_to_talk:
        print("Push-to-talk: hold F8 while speaking. F9 stops speech. Ctrl+C quits.")
    else:
        print("Live mode: just speak. Say 'ruko' or press F9 to stop. Ctrl+C quits.")
        if not app.orchestrator.start_live():
            time.sleep(0.2)
            return
    try:
        while True:
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nStopping…")


def run_window(app: App, start_live: bool) -> None:
    """The customtkinter window (WS7)."""
    from ui.app import run_app

    if start_live:
        app.orchestrator.start_live()
    run_app(app.orchestrator, app.bus, app.settings,
            None if app.settings.sarvam_api_key.startswith("mock") else save_env_values,
            app.audit)


def main(argv: list[str] | None = None) -> int:
    """Load settings, set up logging and start the chosen mode. Returns an exit code."""
    args = parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        # Hindi/Tamil output must not crash when the console or a pipe is not UTF-8.
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        settings = load_settings(require_key=False) if args.mock else get_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    level = logging.DEBUG if args.debug else logging.INFO
    terminal = args.text or args.no_ui
    setup_logging(settings.log_dir, level, console=args.debug or not terminal)
    logger.info("Starting (LLM %s, STT %s, mock=%s)", settings.llm_model,
                settings.stt_realtime_model, args.mock)
    app = build_app(settings, mock=args.mock, text=args.text,
                    kill_switch=not args.no_kill_switch)
    remove_hotkeys: Callable[[], None] = lambda: None  # noqa: E731
    try:
        if args.mock:
            print("MOCK MODE: no Sarvam calls; replies come from keyword rules.")
        if args.text:
            run_text_mode(app)
        else:
            remove_hotkeys = register_hotkeys(app.orchestrator, push_to_talk=args.ptt or args.mock)
            if args.no_ui:
                run_terminal_voice(app, push_to_talk=args.ptt)
            else:
                run_window(app, start_live=not (args.ptt or args.mock))
    finally:
        remove_hotkeys()
        app.close()
        shutdown_logging()
    return 0


if __name__ == "__main__":
    sys.exit(main())

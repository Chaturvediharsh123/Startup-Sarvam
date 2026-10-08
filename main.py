"""Entry point for the Sarvam voice PC assistant.

Usage::

    python main.py            # window + live listening
    python main.py --ptt      # window + push-to-talk instead of live listening
    python main.py --no-ui    # terminal only, live listening
    python main.py --text     # type commands instead of speaking (terminal)
    python main.py --mock     # no Sarvam key needed: keyword "LLM", typed input, window
    python main.py --mock --text   # same, in the terminal
"""

from __future__ import annotations

import argparse
import logging
import queue
import sys
from collections.abc import Callable
from dataclasses import dataclass

from actions import registry as actions
from brain.brain import Brain, Reply
from brain.llm_client import ChatModel, SarvamLLM
from brain.memory import Memory
from core.config import (
    ConfigError,
    Settings,
    get_settings,
    load_settings,
    save_env_values,
)
from core.logging import setup_logging, shutdown_logging
from core.orchestrator import Pipeline, PipelineEvent, create_mock_pipeline, create_pipeline
from safety import guard as safety
from sarvam.client import SarvamClient
from sarvam.mock import MockLLM, MockTTS

logger = logging.getLogger(__name__)

STATUS_MARKS = {"ok": "✓", "error": "✗", "denied": "denied", "blocked": "blocked"}


@dataclass
class Core:
    """The objects every mode needs."""

    settings: Settings
    client: SarvamClient
    memory: Memory
    brain: Brain

    def close(self) -> None:
        """Release the database and HTTP connections."""
        self.memory.close()
        self.client.close()


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
    return parser.parse_args(argv)


def build_core(settings: Settings, mock: bool = False) -> Core:
    """Create the HTTP client, memory, LLM and brain, and configure the safety layer.

    With ``mock=True`` the brain uses :class:`MockLLM`; the HTTP client is created
    but never used.
    """
    actions.configure(settings)
    safety.configure(settings.max_actions_per_minute)
    client = SarvamClient(settings.sarvam_api_key)
    memory = Memory(settings.db_path, settings.default_language)
    llm: ChatModel = (
        MockLLM(memory) if mock
        else SarvamLLM(client, settings.llm_model, settings.llm_reasoning)
    )
    return Core(settings, client, memory, Brain(memory, llm, settings))


def format_reply(reply: Reply) -> str:
    """Human-readable reply with actions and timings, for the terminal."""
    lines = [f"Assistant: {reply.text}"]
    for outcome in reply.actions:
        mark = STATUS_MARKS.get(outcome.status, outcome.status)
        lines.append(f"  [{mark}] {outcome.name}({outcome.args}) -> {outcome.result}")
    t = reply.timings
    lines.append(
        f"  (LLM {t.get('llm_ms', 0) / 1000:.1f}s · action {t.get('action_ms', 0) / 1000:.1f}s"
        f" · total {t.get('total_ms', 0) / 1000:.1f}s · {reply.language})"
    )
    return "\n".join(lines)


def run_text_mode(core: Core, voice: MockTTS | None = None) -> None:
    """Type a command, see the reply. Risky actions ask in the terminal.

    Args:
        voice: in ``--mock`` mode, also speak replies with the offline voice.
    """

    def ask_user(question: str) -> str:
        """Ask in the terminal; end of input counts as no."""
        print(f"Assistant: {question}")
        if voice is not None:
            voice.speak(question)
        try:
            return input("You (confirm): ")
        except EOFError:
            return ""

    print("Text mode. Type a command in any language. 'exit' to quit.")
    while True:
        try:
            line = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if line.lower() in ("exit", "quit", "bye"):
            break
        if line:
            reply = core.brain.handle(line, ask_user=ask_user)
            print(format_reply(reply))
            if voice is not None:
                voice.speak(reply.text)


def register_hotkeys(pipeline: Pipeline, push_to_talk: bool) -> Callable[[], None]:
    """Global F9 = stop speaking; F8 held = push-to-talk. Returns a cleanup function."""
    try:
        import keyboard

        keyboard.add_hotkey("f9", pipeline.stop_speaking)
        if push_to_talk:
            keyboard.on_press_key("f8", lambda _e: pipeline.ptt_press())
            keyboard.on_release_key("f8", lambda _e: pipeline.ptt_release())
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


def print_event(event: PipelineEvent) -> None:
    """Show one pipeline event in the terminal."""
    from ui.app import format_timing

    kind, data = event.kind, event.data
    if kind == "partial":
        print(f"\r  … {data}", end="", flush=True)
    elif kind == "final":
        print(f"\rYou: {data}")
    elif kind == "reply":
        print(f"Assistant: {data}")
    elif kind == "action":
        mark = STATUS_MARKS.get(data.status, data.status)
        print(f"  [{mark}] {data.name}({data.args}) -> {data.result}")
    elif kind == "timing":
        print(f"  ({format_timing(data)})")
    elif kind in ("status", "error"):
        print(f"  [{kind}] {data}")


def run_terminal_voice(pipeline: Pipeline, push_to_talk: bool) -> None:
    """Voice mode without a window. Ctrl+C to quit."""
    if push_to_talk:
        print("Push-to-talk: hold F8 while speaking. F9 stops speech. Ctrl+C quits.")
    else:
        print("Live mode: just speak. F9 stops speech. Ctrl+C quits.")
        if not pipeline.start_live():
            print_event(pipeline.events.get())
            return
    try:
        while True:
            try:
                event = pipeline.events.get(timeout=0.2)
            except queue.Empty:
                continue
            if event.kind != "level":
                print_event(event)
    except KeyboardInterrupt:
        print("\nStopping…")


def run_mock_window(core: Core) -> None:
    """``--mock`` window: typed input, offline voice, no Sarvam calls."""
    from ui.app import run_app

    pipeline = create_mock_pipeline(core.settings, core.brain)
    remove_hotkeys = register_hotkeys(pipeline, push_to_talk=False)
    try:
        run_app(pipeline, core.settings, None, start_live=False, mock=True)
    finally:
        remove_hotkeys()
        pipeline.shutdown()


def run_voice(core: Core, args: argparse.Namespace) -> None:
    """Start the pipeline in the window or the terminal and clean up afterwards."""
    pipeline = create_pipeline(core.settings, core.client, core.brain)
    remove_hotkeys = register_hotkeys(pipeline, push_to_talk=args.ptt)
    try:
        if args.no_ui:
            run_terminal_voice(pipeline, args.ptt)
        else:
            from ui.app import run_app

            run_app(pipeline, core.settings, save_env_values, start_live=not args.ptt)
    finally:
        remove_hotkeys()
        pipeline.shutdown()


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
    logger.info("Starting (LLM %s, STT %s)", settings.llm_model, settings.stt_realtime_model)
    safety.enable_failsafe()
    core = build_core(settings, mock=args.mock)
    try:
        if args.mock:
            print("MOCK MODE: no Sarvam calls; replies come from keyword rules.")
            if terminal:
                run_text_mode(core, voice=MockTTS(settings))
            else:
                run_mock_window(core)
        elif args.text:
            run_text_mode(core)
        else:
            run_voice(core, args)
    finally:
        core.close()
        shutdown_logging()
    return 0


if __name__ == "__main__":
    sys.exit(main())

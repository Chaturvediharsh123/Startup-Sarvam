"""The assistant window (customtkinter).

Large dark-theme widgets for elderly users, with Nirmala UI so Devanagari,
Tamil, Telugu and other Indian scripts render correctly. Widgets are updated
only on the Tk main thread: :meth:`AssistantApp._poll` drains the pipeline
event queue every 50 ms with ``after()``.
"""

from __future__ import annotations

import logging
import queue
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import customtkinter as ctk

from assistant.config import Settings
from assistant.pipeline import Pipeline, PipelineEvent
from assistant.safety import ActionResult

logger = logging.getLogger(__name__)

FONT_FAMILY = "Nirmala UI"
POLL_MS = 50
MAX_EVENTS_PER_POLL = 100
STATUS_MARKS = {"ok": "✓", "error": "✗", "denied": "✗ denied", "blocked": "✗ blocked"}
BULBUL_V3_SPEAKERS = (
    "priya", "ishita", "mani", "roopa", "shubh", "pooja", "sunny", "ratan", "rehan", "ashutosh",
    "amit", "rahul", "aditya", "simran", "suhani", "shreya", "anand", "rupali", "neha", "ritu",
    "manan", "dev", "rohan", "kavya", "sumit", "kabir", "aayan", "advait", "tanya", "tarun",
    "gokul", "vijay", "shruti", "mohit", "kavitha", "soham",
)

SettingsSaver = Callable[[dict[str, str]], None]


def format_timing(timings: dict[str, int]) -> str:
    """Speed readout such as ``STT 0.4s · LLM 0.9s · voice 0.3s · total 1.8s``."""
    parts = [
        ("STT", timings.get("stt_ms")),
        ("LLM", timings.get("llm_ms")),
        ("action", timings.get("action_ms")),
        ("voice", timings.get("tts_first_ms")),
        ("total", timings.get("total_ms")),
    ]
    return " · ".join(f"{name} {ms / 1000:.1f}s" for name, ms in parts if ms)


def format_action(outcome: ActionResult) -> str:
    """One activity-log line for an action outcome."""
    mark = STATUS_MARKS.get(outcome.status, outcome.status)
    args = ", ".join(f"{k}={v}" for k, v in outcome.args.items())
    result = outcome.result if isinstance(outcome.result, str) else "info"
    return f"{mark}  {outcome.name}({args}) — {result}"


class SettingsDialog(ctk.CTkToplevel):
    """Change the speaker voice and wake word."""

    def __init__(self, master: AssistantApp, settings: Settings, on_save: SettingsSaver) -> None:
        super().__init__(master)
        self.title("Settings")
        self.geometry("420x260")
        self.transient(master)
        self._on_save = on_save
        font = ctk.CTkFont(FONT_FAMILY, 18)
        ctk.CTkLabel(self, text="Voice", font=font).grid(row=0, column=0, padx=16, pady=12)
        self.speaker = ctk.CTkOptionMenu(self, values=list(BULBUL_V3_SPEAKERS), font=font)
        self.speaker.set(settings.tts_speaker)
        self.speaker.grid(row=0, column=1, padx=16, pady=12, sticky="ew")
        ctk.CTkLabel(self, text="Wake word", font=font).grid(row=1, column=0, padx=16, pady=12)
        self.wake = ctk.CTkEntry(self, font=font, placeholder_text="empty = off")
        if settings.wake_word:
            self.wake.insert(0, settings.wake_word)
        self.wake.grid(row=1, column=1, padx=16, pady=12, sticky="ew")
        ctk.CTkButton(self, text="Save", font=font, command=self._save).grid(
            row=2, column=0, columnspan=2, padx=16, pady=20, sticky="ew"
        )
        self.grid_columnconfigure(1, weight=1)

    def _save(self) -> None:
        self._on_save({"TTS_SPEAKER": self.speaker.get(), "WAKE_WORD": self.wake.get().strip()})
        self.destroy()


class AssistantApp(ctk.CTk):
    """Main window. Owns no threads; it only reads :attr:`Pipeline.events`.

    Args:
        pipeline: the running pipeline.
        settings: current settings (shown and edited in the settings dialog).
        save_settings: persists changed values (e.g. to ``.env``); optional.
        start_live: switch live mode on at start-up.
        mock: ``--mock`` mode: adds a typing box and disables live mode and
            push-to-talk (both need Sarvam speech-to-text).
    """

    def __init__(
        self,
        pipeline: Pipeline,
        settings: Settings,
        save_settings: SettingsSaver | None = None,
        start_live: bool = True,
        mock: bool = False,
    ) -> None:
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        super().__init__()
        self.pipeline = pipeline
        self.settings = settings
        self._save_settings = save_settings
        self._closing = False
        self.title("Sarvam Sahayak (mock mode)" if mock else "Sarvam Sahayak")
        self.geometry("900x820" if mock else "900x760")
        self.minsize(640, 600)
        self.font = ctk.CTkFont(FONT_FAMILY, 20)
        self.big = ctk.CTkFont(FONT_FAMILY, 26, weight="bold")
        self._build_header()
        self._build_controls()
        self._build_transcript()
        self._build_log()
        if mock:
            self._build_typing()
        self.grid_columnconfigure(0, weight=1)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind("<F9>", lambda _e: self.pipeline.stop_speaking())
        if start_live and not mock:
            self.live_switch.select()
            self._toggle_live()
        self.after(POLL_MS, self._poll)

    def _build_typing(self) -> None:
        """Typed input row (mock mode): Enter or Send submits the text."""
        self.live_switch.configure(state="disabled")
        self.ptt.configure(state="disabled")
        self.status.configure(text="mock mode: type a command")
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.grid(row=9, column=0, padx=20, pady=(0, 16), sticky="ew")
        row.grid_columnconfigure(0, weight=1)
        self.entry = ctk.CTkEntry(row, font=self.font, height=48,
                                  placeholder_text="Type a command, e.g. Notepad kholo")
        self.entry.grid(row=0, column=0, sticky="ew")
        self.entry.bind("<Return>", lambda _e: self._send_typed())
        ctk.CTkButton(row, text="Send", font=self.font, height=48, width=110,
                      command=self._send_typed).grid(row=0, column=1, padx=(8, 0))
        self.entry.focus_set()

    def _send_typed(self) -> None:
        text = self.entry.get()
        self.entry.delete(0, "end")
        self.pipeline.submit_typed(text)

    # -- layout ------------------------------------------------------------------
    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, padx=20, pady=(16, 4), sticky="ew")
        header.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(header, text="Sarvam Sahayak", font=self.big).grid(row=0, column=0)
        self.status = ctk.CTkLabel(header, text="ready", font=self.font, text_color="gray70")
        self.status.grid(row=0, column=1, padx=16, sticky="e")
        self.language = ctk.CTkLabel(header, text="—", font=self.font, width=80)
        self.language.grid(row=0, column=2, sticky="e")

    def _build_controls(self) -> None:
        bar = ctk.CTkFrame(self)
        bar.grid(row=1, column=0, padx=20, pady=8, sticky="ew")
        self.live_switch = ctk.CTkSwitch(
            bar, text="Live mode", font=self.big, command=self._toggle_live,
            switch_width=80, switch_height=40,
        )
        self.live_switch.grid(row=0, column=0, padx=16, pady=12)
        self.ptt = ctk.CTkButton(bar, text="Hold to talk", font=self.font, height=48)
        self.ptt.grid(row=0, column=1, padx=8, pady=12)
        self.ptt.bind("<ButtonPress-1>", lambda _e: self.pipeline.ptt_press())
        self.ptt.bind("<ButtonRelease-1>", lambda _e: self.pipeline.ptt_release())
        ctk.CTkButton(
            bar, text="Stop speaking (F9)", font=self.font, height=48, fg_color="#a33",
            hover_color="#c44", command=self.pipeline.stop_speaking,
        ).grid(row=0, column=2, padx=8, pady=12)
        ctk.CTkButton(bar, text="Settings", font=self.font, height=48, width=110,
                      command=self._open_settings).grid(row=0, column=3, padx=8, pady=12)
        self.level = ctk.CTkProgressBar(bar, height=14)
        self.level.set(0)
        self.level.grid(row=1, column=0, columnspan=4, padx=16, pady=(0, 12), sticky="ew")
        bar.grid_columnconfigure(3, weight=1)

    def _build_transcript(self) -> None:
        ctk.CTkLabel(self, text="You said", font=self.font, anchor="w").grid(
            row=2, column=0, padx=24, sticky="w")
        self.transcript = ctk.CTkTextbox(self, height=140, font=self.font, wrap="word")
        self.transcript.grid(row=3, column=0, padx=20, pady=4, sticky="nsew")
        self.transcript.tag_config("final", foreground="white")
        self.transcript.tag_config("partial", foreground="gray55")
        self.transcript.configure(state="disabled")
        ctk.CTkLabel(self, text="Assistant", font=self.font, anchor="w").grid(
            row=4, column=0, padx=24, pady=(8, 0), sticky="w")
        self.reply = ctk.CTkLabel(self, text="", font=self.big, wraplength=820, justify="left",
                                  anchor="w")
        self.reply.grid(row=5, column=0, padx=24, pady=4, sticky="ew")
        self.speed = ctk.CTkLabel(self, text="", font=self.font, text_color="gray70", anchor="w")
        self.speed.grid(row=6, column=0, padx=24, sticky="w")
        self.grid_rowconfigure(3, weight=1)

    def _build_log(self) -> None:
        ctk.CTkLabel(self, text="Activity", font=self.font, anchor="w").grid(
            row=7, column=0, padx=24, pady=(8, 0), sticky="w")
        self.log = ctk.CTkTextbox(self, height=150, font=ctk.CTkFont(FONT_FAMILY, 16))
        self.log.grid(row=8, column=0, padx=20, pady=(4, 16), sticky="nsew")
        self.log.configure(state="disabled")
        self.grid_rowconfigure(8, weight=1)

    # -- actions ------------------------------------------------------------------
    def _toggle_live(self) -> None:
        if self.live_switch.get():
            if not self.pipeline.start_live():
                self.live_switch.deselect()
            self.ptt.configure(state="disabled")
        else:
            self.pipeline.stop_live()
            self.ptt.configure(state="normal")
            self.status.configure(text="live mode off")

    def _open_settings(self) -> None:
        SettingsDialog(self, self.settings, self._apply_settings)

    def _apply_settings(self, values: dict[str, str]) -> None:
        """Apply new voice / wake word now, and persist them if a saver was given."""
        self.settings = replace(
            self.settings, tts_speaker=values["TTS_SPEAKER"], wake_word=values["WAKE_WORD"]
        )
        self.pipeline.settings = self.settings
        if hasattr(self.pipeline.tts, "settings"):
            self.pipeline.tts.settings = self.settings
        if self._save_settings is not None:
            try:
                self._save_settings(values)
            except OSError as exc:
                self._append(self.log, f"✗ could not save settings: {exc}\n")
        self._append(self.log, f"Settings: voice={values['TTS_SPEAKER']}, "
                               f"wake word={values['WAKE_WORD'] or 'off'}\n")

    # -- event handling (main thread only) -------------------------------------------
    def _poll(self) -> None:
        if self._closing:
            return
        level: float | None = None
        for _ in range(MAX_EVENTS_PER_POLL):
            try:
                event = self.pipeline.events.get_nowait()
            except queue.Empty:
                break
            if event.kind == "level":
                level = float(event.data)  # only the newest level matters
            else:
                self.handle_event(event)
        if level is not None:
            self.level.set(level)
        self.after(POLL_MS, self._poll)

    def handle_event(self, event: PipelineEvent) -> None:
        """Update the widgets for one pipeline event."""
        kind, data = event.kind, event.data
        if kind == "partial":
            self._show_partial(str(data))
        elif kind == "final":
            self._show_final(str(data))
        elif kind == "reply":
            self.reply.configure(text=str(data))
        elif kind == "action":
            self._append(self.log, format_action(data) + "\n")
        elif kind == "timing":
            self.speed.configure(text=format_timing(data))
        elif kind == "status":
            self.status.configure(text=str(data))
            if data == "stopped" and self.live_switch.get():
                self.live_switch.deselect()
                self.ptt.configure(state="normal")
        elif kind == "language":
            self.language.configure(text=str(data))
        elif kind == "error":
            self.status.configure(text="error")
            self._append(self.log, f"✗ {data}\n")

    def _append(self, box: ctk.CTkTextbox, text: str, tag: str | None = None) -> None:
        box.configure(state="normal")
        box.insert("end", text, tag)
        box.see("end")
        box.configure(state="disabled")

    def _replace_partial(self, text: str, tag: str) -> None:
        """Remove the grey partial text and insert ``text`` with ``tag``."""
        self.transcript.configure(state="normal")
        if self.transcript.tag_ranges("partial"):
            self.transcript.delete("partial.first", "partial.last")
        self.transcript.insert("end", text, tag)
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    def _show_partial(self, text: str) -> None:
        self._replace_partial(text, "partial")

    def _show_final(self, text: str) -> None:
        self._replace_partial(text + "\n", "final")

    def on_close(self) -> None:
        """Stop threads, the WebSocket and audio, then close the window."""
        if self._closing:
            return
        self._closing = True
        self.status.configure(text="closing…")
        self.update_idletasks()
        try:
            self.pipeline.shutdown()
        except Exception:  # noqa: BLE001 - always close the window
            logger.exception("Error during shutdown")
        self.destroy()


def run_app(
    pipeline: Pipeline,
    settings: Settings,
    save_settings: SettingsSaver | None = None,
    start_live: bool = True,
    mock: bool = False,
) -> None:
    """Create the window and run the Tk main loop until it is closed."""
    app: Any = AssistantApp(pipeline, settings, save_settings, start_live, mock)
    app.mainloop()

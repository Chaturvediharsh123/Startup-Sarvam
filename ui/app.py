"""The assistant window (PDF WS7): what it heard, what it did, and how fast.

The UI talks to the rest of the app only through the event bus and the
orchestrator's public methods:

* In: every bus event, drained from ``bus.queue()`` every 50 ms with ``after()``
  on the Tk thread and applied to a :class:`~ui.widgets.UiModel`.
* Out: ``interrupt`` (Stop / F9 / Esc), live mode, push-to-talk, typed text,
  wake word and language hint, plus ``save_settings`` for the settings dialog.

Orchestrator calls that may block (start/stop live, push-to-talk, language
hint) run in order on one background worker so the window never freezes;
``interrupt`` is called directly so Stop always works at once.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import queue
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Protocol

import customtkinter as ctk

from core.bus import EventBus
from core.config import UI_THEMES, Settings
from core.interfaces import Health
from ui.labels import UI_LANGUAGE_NAMES, hint_choice, hint_choices, hint_code, t, ui_lang
from ui.theme import MAX_FONT_SIZE, MIN_FONT_SIZE, clamp_font_size, palette
from ui.widgets import (
    ACTIVITY,
    AUDIT,
    AUDIT_LIMIT,
    CONTROLS,
    LEVEL,
    REPLY,
    SPEED,
    STATUS,
    TRANSCRIPT,
    ActivityLog,
    AuditView,
    ControlCallbacks,
    Controls,
    Look,
    ReplyPanel,
    SpeedReadout,
    StatusBar,
    TranscriptView,
    TypedInput,
    UiModel,
    format_action,
    format_timing,
    make_button,
    make_entry,
    make_menu,
)

__all__ = [
    "AssistantApp",
    "AuditSource",
    "OrchestratorAPI",
    "SettingsDialog",
    "apply_settings_values",
    "format_action",
    "format_timing",
    "list_devices",
    "run_app",
]

logger = logging.getLogger(__name__)

POLL_MS = 50
MAX_EVENTS_PER_POLL = 200
BULBUL_V3_SPEAKERS = (
    "priya", "ishita", "mani", "roopa", "shubh", "pooja", "sunny", "ratan", "rehan", "ashutosh",
    "amit", "rahul", "aditya", "simran", "suhani", "shreya", "anand", "rupali", "neha", "ritu",
    "manan", "dev", "rohan", "kavya", "sumit", "kabir", "aayan", "advait", "tanya", "tarun",
    "gokul", "vijay", "shruti", "mohit", "kavitha", "soham",
)
RESTART_KEYS = ("MIC_DEVICE", "SPEAKER_DEVICE", "TTS_SPEAKER")

SettingsSaver = Callable[[dict[str, str]], None]
Done = Callable[[Any, BaseException | None], None]


class OrchestratorAPI(Protocol):
    """The orchestrator methods the window uses."""

    @property
    def live(self) -> bool:
        """True while live listening is on."""
        ...

    def start_live(self) -> bool: ...
    def stop_live(self) -> None: ...
    def ptt_press(self) -> None: ...
    def ptt_release(self) -> None: ...
    def submit_typed(self, text: str) -> None: ...
    def interrupt(self, reason: str = "user") -> None: ...
    def set_language_hint(self, code: str | None) -> None: ...
    def set_wake_word(self, word: str) -> None: ...
    def health(self) -> list[Health]: ...


class AuditSource(Protocol):
    """The safety audit log. Rows have the keys time, turn_id, kind ("decision" or
    "result"), tool, verdict_or_status and reason_or_fact."""

    def recent(self, n: int) -> list[dict[str, Any]]: ...


# -- settings helpers (pure) ------------------------------------------------------------

_SETTING_FIELDS: dict[str, tuple[str, Callable[[str], Any]]] = {
    "MIC_DEVICE": ("mic_device", str),
    "SPEAKER_DEVICE": ("speaker_device", str),
    "TTS_SPEAKER": ("tts_speaker", str),
    "UI_FONT_SIZE": ("ui_font_size", clamp_font_size),
    "UI_THEME": ("ui_theme", lambda v: palette(v).name),
    "UI_LANGUAGE": ("ui_language", ui_lang),
    "WAKE_WORD": ("wake_word", str),
    "LANGUAGE_HINT": ("language_hint", str),
}


def apply_settings_values(settings: Settings, values: dict[str, str]) -> Settings:
    """A copy of ``settings`` with the dialog's ``ENV_NAME -> value`` pairs applied.

    Names the :class:`Settings` class does not have
    are ignored here but still saved to ``.env``.
    """
    known = {f.name for f in dataclasses.fields(settings)}
    changes: dict[str, Any] = {}
    for env_name, value in values.items():
        target = _SETTING_FIELDS.get(env_name)
        if target is not None and target[0] in known:
            changes[target[0]] = target[1](value)
    return dataclasses.replace(settings, **changes)


def list_devices(kind: str) -> list[str] | None:
    """Names of sound devices (``input`` or ``output``), or None if sounddevice is missing."""
    try:
        import sounddevice

        devices = sounddevice.query_devices()
    except Exception:  # noqa: BLE001 - no PortAudio / no devices: fall back to a text box
        return None
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    names: list[str] = []
    for device in devices:
        name = str(device.get("name", ""))
        if name and device.get(key, 0) > 0 and name not in names:
            names.append(name)
    return names


# -- settings dialog --------------------------------------------------------------------


class SettingsDialog(ctk.CTkToplevel):
    """Mic, speaker, voice, text size, colours, screen language, wake word, language hint.

    Not modal, so Stop and F9 keep working while it is open. Enter saves, Esc cancels.
    """

    def __init__(
        self,
        master: Any,
        look: Look,
        settings: Settings,
        on_save: SettingsSaver,
        devices: Callable[[str], list[str] | None] = list_devices,
    ) -> None:
        super().__init__(master)
        self.look = look
        self._on_save = on_save
        pal = look.palette
        self.configure(fg_color=pal.bg)
        self.title(look.t("settings"))
        self.transient(master)
        frame = ctk.CTkFrame(self, fg_color=pal.panel)
        frame.pack(fill="both", expand=True, padx=12, pady=12)
        frame.grid_columnconfigure(1, weight=1)
        bg = pal.panel
        self._row = 0
        self._frame = frame

        default = look.t("default_device")
        self.mic = self._device_field("mic_device", devices("input"), settings.mic_device, default)
        speaker_now = str(getattr(settings, "speaker_device", "") or "")
        self.speaker = self._device_field("speaker_device", devices("output"), speaker_now,
                                          default)
        voices = list(BULBUL_V3_SPEAKERS)
        if settings.tts_speaker not in voices:
            voices.insert(0, settings.tts_speaker)
        self.voice = self._add("voice", make_menu(frame, look, voices, None, bg, width=320))
        self.voice.set(settings.tts_speaker)
        sizes = [str(n) for n in range(MIN_FONT_SIZE, MAX_FONT_SIZE + 1)]
        self.font_size = self._add("font_size", make_menu(frame, look, sizes, None, bg, width=320))
        self.font_size.set(str(clamp_font_size(settings.ui_font_size)))
        self._themes = {look.t(f"theme_{name}"): name for name in UI_THEMES}
        self.theme = self._add("theme", make_menu(frame, look, list(self._themes), None, bg,
                                                  width=320))
        self.theme.set(look.t(f"theme_{palette(settings.ui_theme).name}"))
        self._ui_langs = {name: code for code, name in UI_LANGUAGE_NAMES.items()}
        self.ui_language = self._add("ui_language", make_menu(
            frame, look, list(self._ui_langs), None, bg, width=320))
        self.ui_language.set(UI_LANGUAGE_NAMES[ui_lang(settings.ui_language)])
        self.wake = self._add("wake_word", make_entry(frame, look, look.t("wake_placeholder"),
                                                      width=320))
        if settings.wake_word:
            self.wake.insert(0, settings.wake_word)
        self.hint = self._add("language_hint", make_menu(
            frame, look, hint_choices(look.lang), None, bg, width=320))
        self.hint.set(hint_choice(settings.language_hint, look.lang))

        ctk.CTkLabel(frame, text=look.t("restart_note"), font=look.small, text_color=pal.muted,
                     wraplength=560, justify="left").grid(
            row=self._row, column=0, columnspan=2, padx=16, pady=(8, 0), sticky="w")
        buttons = ctk.CTkFrame(frame, fg_color="transparent")
        buttons.grid(row=self._row + 1, column=0, columnspan=2, padx=12, pady=12, sticky="e")
        make_button(buttons, look, look.t("save"), self.save, bg, width=200).grid(
            row=0, column=0, padx=6)
        make_button(buttons, look, look.t("cancel"), self.destroy, bg, width=200).grid(
            row=0, column=1, padx=6)
        self.bind("<Return>", lambda _e: self.save())
        self.bind("<KP_Enter>", lambda _e: self.save())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.after(100, self._focus_first)

    def _focus_first(self) -> None:
        if self.winfo_exists():
            self.mic.focus_set()

    def _add(self, key: str, widget: Any) -> Any:
        ctk.CTkLabel(self._frame, text=self.look.t(key), font=self.look.base,
                     text_color=self.look.palette.text, anchor="w").grid(
            row=self._row, column=0, padx=16, pady=8, sticky="w")
        widget.grid(row=self._row, column=1, padx=16, pady=8, sticky="ew")
        self._row += 1
        return widget

    def _device_field(self, key: str, names: list[str] | None, current: str, default: str
                      ) -> Any:
        if names is None:
            entry = make_entry(self._frame, self.look, default, width=320)
            if current:
                entry.insert(0, current)
            return self._add(key, entry)
        choices = [default, *names]
        if current and current not in choices:
            choices.append(current)
        menu = make_menu(self._frame, self.look, choices, None, self.look.palette.panel,
                         width=320)
        menu.set(current or default)
        return self._add(key, menu)

    def _device_value(self, widget: Any) -> str:
        value = str(widget.get()).strip()
        return "" if value == self.look.t("default_device") else value

    def values(self) -> dict[str, str]:
        """The chosen settings as ``ENV_NAME -> value`` (what ``.env`` stores)."""
        return {
            "MIC_DEVICE": self._device_value(self.mic),
            "SPEAKER_DEVICE": self._device_value(self.speaker),
            "TTS_SPEAKER": self.voice.get(),
            "UI_FONT_SIZE": str(clamp_font_size(self.font_size.get())),
            "UI_THEME": self._themes.get(self.theme.get(), "dark"),
            "UI_LANGUAGE": self._ui_langs.get(self.ui_language.get(), "en"),
            "WAKE_WORD": self.wake.get().strip(),
            "LANGUAGE_HINT": hint_code(self.hint.get(), self.look.lang) or "",
        }

    def save(self) -> None:
        """Hand the values to the window and close."""
        values = self.values()
        self.destroy()
        self._on_save(values)


# -- main window ------------------------------------------------------------------------


class AssistantApp(ctk.CTk):
    """Main window. It polls ``bus.queue()`` with ``after()``; widgets change on this thread only.

    Args:
        orchestrator: the running orchestrator (see :class:`OrchestratorAPI`).
        bus: the app's event bus; the window receives every event.
        settings: current settings (font size, theme, UI language, wake word, hint...).
        save_settings: persists ``ENV_NAME -> value`` pairs (e.g. ``save_env_values``).
        audit: optional safety audit log with ``recent(n)`` for the safety log view.
    """

    def __init__(
        self,
        orchestrator: OrchestratorAPI,
        bus: EventBus,
        settings: Settings,
        save_settings: SettingsSaver | None = None,
        audit: AuditSource | None = None,
    ) -> None:
        ctk.set_appearance_mode(palette(settings.ui_theme).appearance)
        super().__init__()
        self.orchestrator = orchestrator
        self.bus = bus
        self.settings = settings
        self.audit = audit
        self._save_settings = save_settings
        self._events: queue.Queue[Any] = bus.queue()
        self._results: queue.Queue[tuple[Done | None, Future[Any]]] = queue.Queue()
        self._calls = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ui-call")
        self._closing = False
        self._pending_live = 0
        self._audit_expanded = False
        self._body: ctk.CTkFrame | None = None
        self._renderers: dict[str, Callable[[UiModel], None]] = {}
        self._dialog: SettingsDialog | None = None
        self.model = UiModel(
            live=self._read_live(),
            language_hint=settings.language_hint or None,
            wake_word=settings.wake_word,
        )
        self._seed_health()
        self._load_audit()
        self.geometry("1120x880")
        self.minsize(760, 600)
        self._build()
        self._bind_keys()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll_job = self.after(POLL_MS, self._poll)

    # -- layout -----------------------------------------------------------------------
    def _build(self) -> None:
        """(Re)create every widget with the current theme, font size and UI language."""
        if self._body is not None:
            self._audit_expanded = self.audit_view.expanded
            self._body.destroy()
        look = Look(self.settings.ui_theme, self.settings.ui_font_size, self.settings.ui_language)
        self.look = look
        pal = look.palette
        self.title(look.t("app_title"))
        self.configure(fg_color=pal.bg)
        body = ctk.CTkFrame(self, fg_color=pal.bg, corner_radius=0)
        body.pack(fill="both", expand=True, padx=10, pady=10)
        body.grid_columnconfigure(0, weight=3)
        body.grid_columnconfigure(1, weight=2)
        self._body = body
        pad: dict[str, Any] = {"padx": 6, "pady": 5}

        self.status_bar = StatusBar(body, look)
        self.status_bar.grid(row=0, column=0, columnspan=2, sticky="ew", **pad)
        self.controls = Controls(body, look, ControlCallbacks(
            live=self._on_live, ptt_press=self._ptt_press, ptt_release=self._ptt_release,
            stop=self.stop, wake_word=self._on_wake_word, language_hint=self._on_language_hint,
            settings=self.open_settings,
        ))
        self.controls.grid(row=1, column=0, columnspan=2, sticky="ew", **pad)
        left = ctk.CTkFrame(body, fg_color="transparent")
        left.grid(row=2, column=0, sticky="nsew")
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(0, weight=1)
        self.transcript = TranscriptView(left, look)
        self.transcript.grid(row=0, column=0, sticky="nsew", **pad)
        self.reply = ReplyPanel(left, look)
        self.reply.grid(row=1, column=0, sticky="ew", **pad)
        self.speed = SpeedReadout(left, look)
        self.speed.grid(row=2, column=0, sticky="ew", **pad)
        # Created before the logs so Tab reaches the command box right after the controls.
        self.typed = TypedInput(body, look, self._submit_typed)
        self.typed.grid(row=3, column=0, columnspan=2, sticky="ew", **pad)
        right = ctk.CTkFrame(body, fg_color="transparent")
        right.grid(row=2, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=1)
        self.activity = ActivityLog(right, look)
        self.activity.grid(row=0, column=0, sticky="nsew", **pad)
        refresh = self._load_audit_and_render if self.audit is not None else None
        self.audit_view = AuditView(right, look, refresh, expanded=self._audit_expanded)
        self.audit_view.grid(row=1, column=0, sticky="new", **pad)
        body.grid_rowconfigure(2, weight=1)

        self._renderers = {
            STATUS: self.status_bar.render,
            CONTROLS: self.controls.render,
            LEVEL: self.status_bar.render_level,
            TRANSCRIPT: self.transcript.render,
            REPLY: self.reply.render,
            SPEED: self.speed.render,
            ACTIVITY: self.activity.render,
            AUDIT: self.audit_view.render,
        }
        self._render(set(self._renderers))
        self.entry.focus_set()

    @property
    def entry(self) -> ctk.CTkEntry:
        """The typed-command box."""
        return self.typed.entry

    @property
    def live_switch(self) -> ctk.CTkSwitch:
        """The live mode switch."""
        return self.controls.live_switch

    def _bind_keys(self) -> None:
        # F9 works in every window of the app (also the settings dialog).
        self.bind_all("<F9>", lambda _e: self._key(self.stop))
        self.bind("<Escape>", lambda _e: self._key(self.stop))
        self.bind("<KeyPress-F8>", lambda _e: self._key(self.controls.press_ptt))
        self.bind("<KeyRelease-F8>", lambda _e: self._key(self.controls.release_ptt))
        for seq in ("<Control-l>", "<Control-L>"):
            self.bind(seq, lambda _e: self._key(self.toggle_live))

    @staticmethod
    def _key(action: Callable[[], Any]) -> str:
        action()
        return "break"

    # -- user actions ---------------------------------------------------------------------
    def stop(self) -> None:
        """Stop button / F9 / Esc: interrupt at once, on this thread."""
        self.controls.release_ptt()
        self._safe(self.orchestrator.interrupt, "user")

    def toggle_live(self) -> None:
        """Ctrl+L: flip the live mode switch."""
        self.controls.live_switch.toggle()

    def _on_live(self, on: bool) -> None:
        self._pending_live += 1
        self.model.live = on
        call = self.orchestrator.start_live if on else self.orchestrator.stop_live
        self._background(call, done=lambda value, err: self._live_done(on, value, err))

    def _live_done(self, on: bool, value: Any, err: BaseException | None) -> None:
        self._pending_live = max(0, self._pending_live - 1)
        if on and (err is not None or not value):
            self.model.detail_kind = "error"
            self.model.detail = self.look.t("live_failed") + (f" ({err})" if err else "")
        self.model.live = self._read_live() if err is not None else bool(on and value)
        self._render({CONTROLS, STATUS})

    def _ptt_press(self) -> None:
        self._background(self.orchestrator.ptt_press)

    def _ptt_release(self) -> None:
        self._background(self.orchestrator.ptt_release)

    def _submit_typed(self, text: str) -> None:
        self._safe(self.orchestrator.submit_typed, text)

    def _send_typed(self) -> None:
        """Send whatever is in the typed-command box (same as pressing Enter)."""
        self.typed.send()

    def _on_wake_word(self, word: str) -> None:
        self.model.wake_word = word
        self._safe(self.orchestrator.set_wake_word, word)

    def _on_language_hint(self, code: str | None) -> None:
        self.model.language_hint = code
        self._background(self.orchestrator.set_language_hint, code)

    def open_settings(self) -> None:
        """Open the settings dialog (or bring the open one to the front)."""
        if self._dialog is not None and self._dialog.winfo_exists():
            self._dialog.focus_set()
            return
        self._dialog = SettingsDialog(self, self.look, self.settings, self.apply_settings)

    def apply_settings(self, values: dict[str, str]) -> None:
        """Persist the dialog's values; apply theme, text size, labels, wake word and hint."""
        old = self.settings
        self.settings = apply_settings_values(old, values)
        lang = self.settings.ui_language
        if self._save_settings is not None:
            try:
                self._save_settings(values)
            except Exception as exc:  # noqa: BLE001 - a failed save must not close the window
                logger.exception("Could not save settings")
                self._set_detail("error", t("save_failed", lang, error=exc))
            else:
                note = t("saved", lang)
                if any(values.get(k, "") != _current(old, k) for k in RESTART_KEYS if k in values):
                    note += ". " + t("restart_note", lang)
                self._set_detail("status", note)
        wake = values.get("WAKE_WORD", self.model.wake_word)
        if wake != self.model.wake_word:
            self._on_wake_word(wake)
        hint = values.get("LANGUAGE_HINT", self.model.language_hint or "") or None
        if hint != self.model.language_hint:
            self._on_language_hint(hint)
        looks = ("ui_theme", "ui_font_size", "ui_language")
        if any(getattr(old, k) != getattr(self.settings, k) for k in looks):
            ctk.set_appearance_mode(palette(self.settings.ui_theme).appearance)
            self._build()
        else:
            self._render({STATUS, CONTROLS})

    # -- event handling (Tk thread only) ------------------------------------------------
    def _poll(self) -> None:
        if self._closing:
            return
        try:
            changed: set[str] = set()
            for _ in range(MAX_EVENTS_PER_POLL):
                try:
                    event = self._events.get_nowait()
                except queue.Empty:
                    break
                try:
                    changed |= self.model.apply(event)
                except Exception:  # noqa: BLE001 - one bad event must not stop the window
                    logger.exception("UI could not apply %r", event)
            self._drain_results()
            if changed - {LEVEL} and not self._pending_live:
                live = self._read_live()
                if live != self.model.live:
                    self.model.live = live
                    changed.add(CONTROLS)
            self._render(changed)
        except Exception:  # noqa: BLE001 - the poll loop must keep running
            logger.exception("UI poll failed")
        finally:
            if not self._closing:
                self._poll_job = self.after(POLL_MS, self._poll)

    def handle_event(self, event: Any) -> None:
        """Apply one event and redraw at once (for tests and direct callers)."""
        self._render(set(self.model.apply(event)))

    def _render(self, sections: set[str] | frozenset[str]) -> None:
        for section in sections:
            render = self._renderers.get(section)
            if render is None:
                continue
            try:
                render(self.model)
            except Exception:  # noqa: BLE001 - a drawing bug must not freeze the window
                logger.exception("UI could not draw %s", section)

    def _drain_results(self) -> None:
        while True:
            try:
                done, future = self._results.get_nowait()
            except queue.Empty:
                return
            err = future.exception() if not future.cancelled() else None
            value = None if err is not None or future.cancelled() else future.result()
            if err is not None:
                logger.error("Orchestrator call failed: %s", err)
            if done is not None:
                done(value, err)
            elif err is not None:
                self._set_detail("error", str(err))

    # -- helpers ------------------------------------------------------------------------
    def _background(self, call: Callable[..., Any], *args: Any, done: Done | None = None) -> None:
        """Run a possibly slow orchestrator call off the Tk thread (calls keep their order)."""
        if self._closing:
            return
        try:
            future = self._calls.submit(call, *args)
        except RuntimeError:  # the worker was shut down: the window is closing
            return
        future.add_done_callback(lambda f: self._results.put((done, f)))

    def _safe(self, call: Callable[..., Any], *args: Any) -> Any:
        """Call the orchestrator directly; show (never raise) a failure."""
        try:
            return call(*args)
        except Exception as exc:  # noqa: BLE001 - the window must survive any module bug
            logger.exception("Orchestrator call failed")
            self._set_detail("error", str(exc))
            return None

    def _set_detail(self, kind: str, text: str) -> None:
        self.model.detail_kind, self.model.detail = kind, text
        self._render({STATUS})

    def _read_live(self) -> bool:
        try:
            return bool(self.orchestrator.live)
        except Exception:  # noqa: BLE001 - treat a broken orchestrator as "not live"
            return False

    def _seed_health(self) -> None:
        try:
            for health in self.orchestrator.health():
                self.model.set_health(health.module, bool(health.ok), health.detail)
        except Exception:  # noqa: BLE001 - health dots stay grey until HealthChanged
            logger.exception("Could not read module health")

    def _load_audit(self) -> None:
        if self.audit is None:
            return
        try:
            self.model.set_audit(self.audit.recent(AUDIT_LIMIT))
        except Exception:  # noqa: BLE001 - live decisions from the bus still show
            logger.exception("Could not read the safety audit log")

    def _load_audit_and_render(self) -> None:
        self._load_audit()
        self._render({AUDIT})

    def on_close(self) -> None:
        """Close the window. The caller (``main``) shuts the orchestrator down afterwards."""
        if self._closing:
            return
        self._closing = True
        self._calls.shutdown(wait=False, cancel_futures=True)
        with contextlib.suppress(Exception):
            self.after_cancel(self._poll_job)
        self.unbind_all("<F9>")
        self.destroy()


def _current(settings: Settings, env_name: str) -> str:
    """The current value of a setting by its ``.env`` name, as text."""
    name = _SETTING_FIELDS[env_name][0]
    return str(getattr(settings, name, "") or "")


def run_app(
    orchestrator: OrchestratorAPI,
    bus: EventBus,
    settings: Settings,
    save_settings: SettingsSaver | None = None,
    audit: AuditSource | None = None,
) -> None:
    """Create the window and run the Tk main loop until it is closed."""
    app = AssistantApp(orchestrator, bus, settings, save_settings, audit)
    app.after(0, lambda: _maximise(app))
    app.mainloop()


def _maximise(window: Any) -> None:
    """Fill the screen (big text needs the room); ignore window managers that refuse."""
    try:
        window.state("zoomed")  # Windows
    except Exception:  # noqa: BLE001 - e.g. X11 has no "zoomed" state
        with contextlib.suppress(Exception):
            window.attributes("-zoomed", True)

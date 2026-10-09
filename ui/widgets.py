"""The window's parts (PDF WS7) and the pure state they draw.

Two layers:

* :class:`UiModel` holds everything the window shows. :meth:`UiModel.apply`
  maps one bus event to model changes and returns which sections changed. It
  and the ``format_*`` helpers are pure Python, so tests need no display.
* The ``CTkFrame`` widgets (:class:`StatusBar`, :class:`TranscriptView`,
  :class:`ReplyPanel`, :class:`ActivityLog`, :class:`AuditView`,
  :class:`SpeedReadout`, :class:`Controls`, :class:`TypedInput`) only draw a
  model with ``render(model)``. They are touched on the Tk thread only.
"""

from __future__ import annotations

import contextlib
import time
import tkinter
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import customtkinter as ctk

from core.events import (
    ActionResult,
    BrainResult,
    ErrorRaised,
    FinalTranscript,
    HealthChanged,
    Interrupt,
    LanguageDetected,
    Metrics,
    MicLevel,
    ModeChanged,
    PartialTranscript,
    SafetyDecision,
    SpeakRequest,
    StateChanged,
    Status,
    TurnState,
    Verdict,
)
from ui.labels import hint_choice, hint_choices, hint_code, t
from ui.theme import Palette, font_spec, health_colour, palette, status_colour

HEALTH_MODULES = ("audio", "stt", "brain", "actions", "safety", "tts")
ACTIVITY_LIMIT = 20
AUDIT_LIMIT = 50
TRANSCRIPT_LINES = 30
STATUS_MARKS = {
    "ok": "✓", "error": "✗", "denied": "✗", "blocked": "✗", "timeout": "✗", "cancelled": "–",
}
HEALTH_MARKS = {True: "✓", False: "✗", None: "…"}

# Sections of the window, returned by UiModel.apply so only those are redrawn.
STATUS = "status"
TRANSCRIPT = "transcript"
REPLY = "reply"
ACTIVITY = "activity"
AUDIT = "audit"
SPEED = "speed"
LEVEL = "level"
CONTROLS = "controls"


# -- pure helpers ---------------------------------------------------------------------


def clock(ts: Any) -> str:
    """``HH:MM:SS`` for a Unix timestamp; any other value is shown as text."""
    if isinstance(ts, int | float) and not isinstance(ts, bool):
        return time.strftime("%H:%M:%S", time.localtime(ts))
    text = str(ts or "")
    if len(text) >= 19 and text[10] in "T ":  # ISO date-time: keep the time of day
        return text[11:19]
    return text


def state_text(state: TurnState | str, lang: str = "en") -> str:
    """Friendly words for an orchestrator state, e.g. ``Listening…``."""
    value = state.value if isinstance(state, TurnState) else str(state)
    return t(f"state_{value}", lang)


def status_word(status: str, lang: str = "en") -> str:
    """Translated action status or safety verdict (unknown values are shown as is)."""
    for prefix in ("status_", "verdict_"):
        key = f"{prefix}{status}"
        if t(key, lang) != key:
            return t(key, lang)
    return status


def outcome_text(outcome: Any) -> str:
    """The short fact of an action result, or its text result, or ``""``."""
    fact = getattr(outcome, "fact", "") or ""
    if fact:
        return str(fact)
    result = getattr(outcome, "result", None)
    return result if isinstance(result, str) else ""


def format_action(outcome: Any) -> str:
    """One terminal / log line for an action result, e.g. ``✓  open_app(name=notepad) — …``."""
    mark = STATUS_MARKS.get(outcome.status, outcome.status)
    if outcome.status in ("denied", "blocked"):
        mark = f"{mark} {outcome.status}"
    args = ", ".join(f"{k}={v}" for k, v in (getattr(outcome, "args", None) or {}).items())
    return f"{mark}  {outcome.name}({args}) — {outcome_text(outcome) or 'info'}"


_SPEED_FIELDS = (
    ("speed_stt", "stt_ms"),
    ("speed_llm", "llm_ms"),
    ("speed_action", "action_ms"),
    ("speed_first_audio", "tts_first_ms"),
    ("speed_total", "total_ms"),
)
SPEED_TILES = ("stt_ms", "llm_ms", "tts_first_ms", "total_ms")


def metric_values(metrics: Metrics | Mapping[str, Any] | None) -> dict[str, int]:
    """``{"stt_ms": 400, ...}`` from a :class:`Metrics` event or a plain dict."""
    if metrics is None:
        return {}
    get = metrics.get if isinstance(metrics, Mapping) else lambda k: getattr(metrics, k, 0)
    return {key: int(get(key) or 0) for _, key in _SPEED_FIELDS}


def format_timing(metrics: Metrics | Mapping[str, Any] | None, lang: str = "en") -> str:
    """Speed readout such as ``STT 400 ms · LLM 900 ms · first audio 300 ms · total 1600 ms``.

    Zero or missing timings are left out.
    """
    values = metric_values(metrics)
    unit = t("ms", lang)
    return " · ".join(
        f"{t(label, lang)} {values[key]} {unit}" for label, key in _SPEED_FIELDS if values.get(key)
    )


def decision_entry(decision: SafetyDecision) -> dict[str, Any]:
    """A safety decision as an audit row (same keys as the audit log's ``recent()``)."""
    verdict = decision.verdict
    return {
        "time": decision.ts,
        "turn_id": decision.turn_id,
        "kind": "decision",
        "tool": decision.tool_call.name,
        "verdict_or_status": verdict.value if isinstance(verdict, Verdict) else str(verdict),
        "reason_or_fact": decision.reason or decision.question,
    }


def format_activity(outcome: ActionResult, lang: str = "en") -> str:
    """Activity log line: time, name, status and fact."""
    mark = STATUS_MARKS.get(outcome.status, "•")
    text = outcome_text(outcome)
    line = f"{clock(outcome.ts)}  {mark} {outcome.name} — {status_word(outcome.status, lang)}"
    return f"{line}: {text}" if text else line


def format_audit(entry: Mapping[str, Any], lang: str = "en") -> str:
    """Safety log line: time, tool, verdict (or result) and the reason."""
    verdict = str(entry.get("verdict_or_status", ""))
    reason = str(entry.get("reason_or_fact", "") or "")
    line = f"{clock(entry.get('time'))}  {entry.get('tool', '')} — {status_word(verdict, lang)}"
    return f"{line}: {reason}" if reason else line


def audit_tag(entry: Mapping[str, Any]) -> str:
    """Colour tag for an audit row: ``bad`` for denials and failures, else ``ok``/``warn``."""
    value = str(entry.get("verdict_or_status", ""))
    if value in ("deny", "denied", "blocked", "error", "timeout"):
        return "bad"
    if value in ("confirm", "cancelled"):
        return "warn"
    return "ok"


@dataclass
class UiModel:
    """Everything the window shows. Changed only by :meth:`apply` and the app."""

    state: TurnState = TurnState.IDLE
    detail_kind: str = ""  # "" | "status" | "error" | "stopped"
    detail: str = ""
    language: str = ""
    health: dict[str, bool | None] = field(default_factory=lambda: dict.fromkeys(HEALTH_MODULES))
    health_detail: dict[str, str] = field(default_factory=dict)
    partial: str = ""
    finals: deque[str] = field(default_factory=lambda: deque(maxlen=TRANSCRIPT_LINES))
    said: str = ""
    did: str = ""
    did_status: str = ""
    activity: deque[ActionResult] = field(default_factory=lambda: deque(maxlen=ACTIVITY_LIMIT))
    audit: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=AUDIT_LIMIT))
    metrics: Metrics | None = None
    level: float = 0.0
    live: bool = False
    language_hint: str | None = None
    wake_word: str = ""

    def set_health(self, module: str, ok: bool | None, detail: str = "") -> None:
        """Record one module's health (unknown modules get their own dot)."""
        self.health[module] = ok
        self.health_detail[module] = detail

    def set_audit(self, entries: Iterable[Mapping[str, Any]]) -> None:
        """Replace the safety log with rows from the audit store (any order)."""
        rows = [dict(e) for e in entries]
        with contextlib.suppress(TypeError):  # mixed time formats: keep the store's order
            rows.sort(key=lambda row: row.get("time") or 0)
        self.audit.clear()
        self.audit.extend(rows)

    def apply(self, event: Any) -> frozenset[str]:
        """Update the model for one bus event; return the sections to redraw."""
        if isinstance(event, MicLevel):
            self.level = max(0.0, min(1.0, float(event.level)))
            return frozenset({LEVEL})
        if isinstance(event, PartialTranscript):
            self.partial = event.text
            return frozenset({TRANSCRIPT})
        if isinstance(event, FinalTranscript):
            self.partial = ""
            if event.text:
                self.finals.append(event.text)
            self.said, self.did, self.did_status = "", "", ""
            if self.detail_kind in ("error", "stopped"):
                self.detail_kind, self.detail = "", ""
            if event.language:
                self.language = event.language
            return frozenset({TRANSCRIPT, REPLY, STATUS})
        if isinstance(event, StateChanged):
            self.state = TurnState(event.new)
            return frozenset({STATUS})
        if isinstance(event, HealthChanged):
            self.set_health(event.module, bool(event.ok), event.detail)
            return frozenset({STATUS})
        if isinstance(event, LanguageDetected):
            self.language = event.language
            return frozenset({STATUS})
        if isinstance(event, Status):
            self.detail_kind, self.detail = "status", event.text
            return frozenset({STATUS})
        if isinstance(event, ErrorRaised):
            prefix = f"{event.module}: " if event.module else ""
            self.detail_kind, self.detail = "error", prefix + event.message
            return frozenset({STATUS})
        if isinstance(event, Interrupt):
            self.detail_kind, self.detail = "stopped", event.reason
            self.partial = ""
            return frozenset({STATUS, TRANSCRIPT})
        if isinstance(event, SpeakRequest):
            self.said = event.text
            return frozenset({REPLY})
        if isinstance(event, BrainResult):
            self.did, self.did_status = "", ""
            if event.language:
                self.language = event.language
            return frozenset({REPLY, STATUS})
        if isinstance(event, SafetyDecision):
            self.audit.append(decision_entry(event))
            if event.verdict == Verdict.DENY:
                self.did = f"{event.tool_call.name}: {event.reason}".rstrip(": ")
                self.did_status = "denied"
                return frozenset({AUDIT, REPLY})
            return frozenset({AUDIT})
        if isinstance(event, ActionResult):
            self.activity.append(event)
            text = outcome_text(event)
            self.did = f"{event.name}: {text}" if text else event.name
            self.did_status = event.status
            return frozenset({ACTIVITY, REPLY})
        if isinstance(event, Metrics):
            self.metrics = event
            return frozenset({SPEED})
        if isinstance(event, ModeChanged):
            if event.live is not None:
                self.live = bool(event.live)
            if event.language_hint is not None:
                self.language_hint = event.language_hint or None
            return frozenset({CONTROLS})
        return frozenset()


# -- Tk widgets -----------------------------------------------------------------------


class Look:
    """Palette, fonts and UI language shared by every widget. Create after the Tk root."""

    def __init__(self, theme: str, font_size: int, lang: str) -> None:
        self.palette: Palette = palette(theme)
        self.sizes = font_spec(font_size)
        self.lang = lang
        family = self.sizes.family
        self.base = ctk.CTkFont(family, self.sizes.base)
        self.bold = ctk.CTkFont(family, self.sizes.base, weight="bold")
        self.big = ctk.CTkFont(family, self.sizes.big, weight="bold")
        self.small = ctk.CTkFont(family, self.sizes.small)

    def t(self, key: str, **values: object) -> str:
        """Label in the current UI language."""
        return t(key, self.lang, **values)


def _keys(widget: Any, sequences: Iterable[str], handler: Callable[[], Any]) -> None:
    """Bind keys on the widget's own Tk frame (works for every CTk widget)."""

    def run(_event: Any) -> str:
        handler()
        return "break"

    for seq in sequences:
        tkinter.Misc.bind(widget, seq, run, add="+")


def make_focusable(widget: Any, look: Look, bg: str, activate: Callable[[], Any] | None = None
                   ) -> Any:
    """Let Tab reach a CTk widget, draw a thick focus ring, and activate it with Enter/Space."""
    pal = look.palette
    tkinter.Frame.configure(
        widget, takefocus=1, highlightthickness=pal.focus_width,
        highlightcolor=pal.focus, highlightbackground=bg,
    )
    if activate is not None:
        _keys(widget, ("<Return>", "<KP_Enter>", "<space>"), activate)
    return widget


def _focus_set(widget: Any) -> None:
    tkinter.Misc.focus_set(widget)


def heading(parent: Any, look: Look, key: str) -> ctk.CTkLabel:
    """A section heading label."""
    return ctk.CTkLabel(parent, text=look.t(key), font=look.bold,
                        text_color=look.palette.heading, anchor="w")


def make_button(parent: Any, look: Look, text: str, command: Callable[[], Any] | None,
                bg: str, danger: bool = False, width: int = 140) -> ctk.CTkButton:
    """A large, keyboard-reachable button."""
    pal = look.palette
    button = ctk.CTkButton(
        parent, text=text, font=look.bold if danger else look.base, height=52, width=width,
        command=command,
        fg_color=pal.danger if danger else pal.accent,
        hover_color=pal.danger_hover if danger else pal.accent_hover,
        text_color=pal.danger_text if danger else pal.accent_text,
        border_width=pal.border_width if pal.name == "high_contrast" else 0,
        border_color=pal.text,
    )
    make_focusable(button, look, bg, button.invoke if command else None)
    # CTkButton.focus_set focuses its inner label; send focus to the ringed frame instead.
    button.focus_set = lambda: _focus_set(button)  # type: ignore[method-assign]
    return button


def make_switch(parent: Any, look: Look, text: str, command: Callable[[], Any], bg: str
                ) -> ctk.CTkSwitch:
    """A large on/off switch; Space or Enter toggles it."""
    pal = look.palette
    switch = ctk.CTkSwitch(
        parent, text=text, font=look.bold, command=command, switch_width=72, switch_height=36,
        progress_color=pal.ok, fg_color=pal.track, button_color=pal.text,
        button_hover_color=pal.muted, text_color=pal.text,
    )
    make_focusable(switch, look, bg, switch.toggle)
    switch.focus_set = lambda: _focus_set(switch)  # type: ignore[method-assign]
    return switch


def make_menu(parent: Any, look: Look, values: list[str], command: Callable[[str], Any] | None,
              bg: str, width: int = 220) -> ctk.CTkOptionMenu:
    """A dropdown; Up/Down change the value, Enter/Space open the list."""
    pal = look.palette
    menu = ctk.CTkOptionMenu(
        parent, values=values, font=look.base, dropdown_font=look.base, height=48, width=width,
        command=command, fg_color=pal.accent, button_color=pal.accent_hover,
        button_hover_color=pal.accent_hover, text_color=pal.accent_text,
        dropdown_fg_color=pal.panel, dropdown_text_color=pal.text,
        dropdown_hover_color=pal.accent,
    )

    def step(delta: int) -> None:
        options = list(menu.cget("values"))
        if not options:
            return
        index = options.index(menu.get()) if menu.get() in options else 0
        value = options[(index + delta) % len(options)]
        menu.set(value)
        if command is not None:
            command(value)

    def open_list() -> None:
        opener = getattr(menu, "_open_dropdown_menu", None)
        if callable(opener):
            opener()

    make_focusable(menu, look, bg, open_list)
    _keys(menu, ("<Down>", "<Right>"), lambda: step(1))
    _keys(menu, ("<Up>", "<Left>"), lambda: step(-1))
    menu.focus_set = lambda: _focus_set(menu)  # type: ignore[method-assign]
    return menu


def make_entry(parent: Any, look: Look, placeholder: str = "", width: int = 200) -> ctk.CTkEntry:
    """A text entry whose border turns into the focus ring while it has focus."""
    pal = look.palette
    idle = {"border_color": pal.muted, "border_width": 2}
    entry = ctk.CTkEntry(
        parent, font=look.base, height=52, width=width, placeholder_text=placeholder,
        fg_color=pal.entry_bg, text_color=pal.text, placeholder_text_color=pal.muted, **idle,
    )
    entry.bind("<FocusIn>", lambda _e: entry.configure(border_color=pal.focus,
                                                       border_width=pal.focus_width + 1))
    entry.bind("<FocusOut>", lambda _e: entry.configure(**idle))
    return entry


def make_textbox(parent: Any, look: Look, height: int, font: Any = None) -> ctk.CTkTextbox:
    """A read-only text area. It is skipped by Tab so keyboard users do not get stuck in it."""
    pal = look.palette
    box = ctk.CTkTextbox(
        parent, height=height, font=font or look.base, wrap="word", takefocus=0,
        fg_color=pal.entry_bg, text_color=pal.text, border_width=pal.border_width,
        border_color=pal.muted,
    )
    for tag, colour in (("ok", pal.ok), ("bad", pal.bad), ("warn", pal.warn),
                        ("final", pal.final), ("partial", pal.partial), ("muted", pal.muted)):
        box.tag_config(tag, foreground=colour)
    box.configure(state="disabled")
    return box


def write_lines(box: ctk.CTkTextbox, lines: Iterable[tuple[str, str]], end: bool = False) -> None:
    """Replace a read-only text box's content with ``(text, tag)`` pairs."""
    box.configure(state="normal")
    box.delete("1.0", "end")
    for text, tag in lines:
        box.insert("end", text, tag)
    box.configure(state="disabled")
    box.see("end" if end else "1.0")


class Section(ctk.CTkFrame):
    """A panel with the section background colour."""

    def __init__(self, master: Any, look: Look) -> None:
        pal = look.palette
        super().__init__(master, fg_color=pal.panel, corner_radius=10,
                         border_width=pal.border_width if pal.name == "high_contrast" else 0,
                         border_color=pal.text)
        self.look = look
        self.bg = pal.panel


class StatusBar(Section):
    """Big state words (Listening / Thinking / Speaking / Waiting for yes), the mic level
    meter, the current language, a detail line and one health dot per module."""

    def __init__(self, master: Any, look: Look) -> None:
        super().__init__(master, look)
        pal = look.palette
        self.state_label = ctk.CTkLabel(self, text="", font=look.big, text_color=pal.text,
                                        anchor="w")
        self.state_label.grid(row=0, column=0, padx=16, pady=(8, 0), sticky="w")
        meter = ctk.CTkFrame(self, fg_color="transparent")
        meter.grid(row=0, column=1, padx=8, pady=(8, 0), sticky="e")
        ctk.CTkLabel(meter, text=look.t("mic_level"), font=look.small,
                     text_color=pal.muted).grid(row=0, column=0, padx=(0, 8))
        self.level = ctk.CTkProgressBar(meter, height=16, width=220, progress_color=pal.ok,
                                        fg_color=pal.track)
        self.level.set(0)
        self.level.grid(row=0, column=1)
        self.language_label = ctk.CTkLabel(self, text="", font=look.bold, text_color=pal.heading)
        self.language_label.grid(row=0, column=2, padx=16, pady=(8, 0), sticky="e")
        self.detail_label = ctk.CTkLabel(self, text="", font=look.base, text_color=pal.muted,
                                         anchor="w", justify="left", wraplength=420)
        self.detail_label.grid(row=1, column=0, padx=16, pady=(0, 8), sticky="ew")
        dots = ctk.CTkFrame(self, fg_color="transparent")
        dots.grid(row=1, column=1, columnspan=2, padx=10, pady=(0, 8), sticky="e")
        self.dots: dict[str, ctk.CTkLabel] = {}
        self._dot_frame = dots
        self.grid_columnconfigure(0, weight=1)
        self.detail_label.bind("<Configure>", self._wrap)

    def _wrap(self, event: Any) -> None:
        self.detail_label.configure(wraplength=max(200, int(event.width) - 8))

    def render_level(self, model: UiModel) -> None:
        """Draw the mic level meter."""
        self.level.set(model.level)

    def _dot(self, module: str) -> ctk.CTkLabel:
        label = self.dots.get(module)
        if label is None:
            label = ctk.CTkLabel(self._dot_frame, text="", font=self.look.base)
            label.grid(row=0, column=len(self.dots), padx=(6, 14))
            self.dots[module] = label
        return label

    def render(self, model: UiModel) -> None:
        """Draw the state, detail line, language and health dots."""
        look, pal = self.look, self.look.palette
        awaiting = model.state == TurnState.AWAITING_YES
        self.state_label.configure(text=state_text(model.state, look.lang),
                                   text_color=pal.warn if awaiting else pal.text)
        if model.detail_kind == "error":
            text, colour = look.t("problem", message=model.detail), pal.bad
        elif awaiting:
            text, colour = look.t("yes_no_hint"), pal.warn
        elif model.detail_kind == "stopped":
            text, colour = look.t("stopped"), pal.muted
        else:
            text, colour = model.detail, pal.muted
        self.detail_label.configure(text=text, text_color=colour)
        language = f"{look.t('language_now')}: {model.language}" if model.language else ""
        self.language_label.configure(text=language)
        for module, ok in model.health.items():
            name = look.t(f"mod_{module}")
            if name == f"mod_{module}":
                name = module
            self._dot(module).configure(text=f"● {name} {HEALTH_MARKS[ok]}",
                                        text_color=health_colour(pal, ok))


class TranscriptView(Section):
    """What the assistant heard: grey partial text, replaced by strong final text."""

    def __init__(self, master: Any, look: Look) -> None:
        super().__init__(master, look)
        heading(self, look, "you_said").grid(row=0, column=0, padx=16, pady=(8, 0), sticky="w")
        self.box = make_textbox(self, look, height=90)
        self.box.grid(row=1, column=0, padx=10, pady=(4, 10), sticky="nsew")
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

    def render(self, model: UiModel) -> None:
        """Draw the last final sentences and the current partial text."""
        lines = [(text + "\n", "final") for text in model.finals]
        if model.partial:
            lines.append((model.partial, "partial"))
        write_lines(self.box, lines, end=True)

    def text(self) -> str:
        """Everything shown (used by tests)."""
        return self.box.get("1.0", "end").strip()


class ReplyPanel(Section):
    """What the assistant said (big) and what it did (coloured by result)."""

    def __init__(self, master: Any, look: Look) -> None:
        super().__init__(master, look)
        pal = look.palette
        heading(self, look, "assistant_said").grid(row=0, column=0, padx=16, pady=(8, 0),
                                                   sticky="w")
        self.said = ctk.CTkLabel(self, text="", font=look.big, text_color=pal.text,
                                 anchor="w", justify="left", wraplength=600)
        self.said.grid(row=1, column=0, padx=16, sticky="ew")
        heading(self, look, "assistant_did").grid(row=2, column=0, padx=16, pady=(8, 0),
                                                  sticky="w")
        self.did = ctk.CTkLabel(self, text="", font=look.base, text_color=pal.text,
                                anchor="w", justify="left", wraplength=600)
        self.did.grid(row=3, column=0, padx=16, pady=(0, 10), sticky="ew")
        self.grid_columnconfigure(0, weight=1)
        self.bind("<Configure>", self._wrap)

    def _wrap(self, event: Any) -> None:
        width = max(200, int(event.width) - 48)
        self.said.configure(wraplength=width)
        self.did.configure(wraplength=width)

    def render(self, model: UiModel) -> None:
        """Draw the spoken reply and the action outcome."""
        pal = self.look.palette
        self.said.configure(text=model.said or "—")
        if model.did:
            mark = STATUS_MARKS.get(model.did_status, "")
            word = status_word(model.did_status, self.look.lang) if model.did_status else ""
            text = f"{mark} {model.did}" + (f" ({word})" if word else "")
            self.did.configure(text=text.strip(), text_color=status_colour(pal, model.did_status))
        else:
            self.did.configure(text=self.look.t("nothing_yet"), text_color=pal.muted)


class SpeedReadout(Section):
    """STT, LLM, first audio and total time of the last turn, in milliseconds."""

    LABELS = {"stt_ms": "speed_stt", "llm_ms": "speed_llm", "tts_first_ms": "speed_first_audio",
              "total_ms": "speed_total"}

    def __init__(self, master: Any, look: Look) -> None:
        super().__init__(master, look)
        pal = look.palette
        heading(self, look, "speed").grid(row=0, column=0, padx=(16, 8), pady=8, sticky="w")
        self.values: dict[str, ctk.CTkLabel] = {}
        for index, key in enumerate(SPEED_TILES):
            ctk.CTkLabel(self, text=look.t(self.LABELS[key]), font=look.small,
                         text_color=pal.muted).grid(row=0, column=1 + 2 * index, padx=(12, 4))
            value = ctk.CTkLabel(self, text="—", font=look.bold, text_color=pal.text)
            value.grid(row=0, column=2 + 2 * index, padx=(0, 8))
            self.values[key] = value

    def render(self, model: UiModel) -> None:
        """Draw the timings of the last :class:`Metrics` event."""
        values = metric_values(model.metrics)
        unit = self.look.t("ms")
        for key, label in self.values.items():
            ms = values.get(key, 0)
            label.configure(text=f"{ms} {unit}" if ms else "—")


class ActivityLog(Section):
    """The last 20 actions, newest first: time, name, result and fact."""

    def __init__(self, master: Any, look: Look) -> None:
        super().__init__(master, look)
        heading(self, look, "activity").grid(row=0, column=0, padx=16, pady=(8, 0), sticky="w")
        self.box = make_textbox(self, look, height=120, font=look.small)
        self.box.grid(row=1, column=0, padx=10, pady=(4, 10), sticky="nsew")
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

    def render(self, model: UiModel) -> None:
        """Draw the activity rows."""
        lang = self.look.lang
        rows = [
            (format_activity(o, lang) + "\n", "ok" if o.ok else "bad")
            for o in reversed(model.activity)
        ]
        write_lines(self.box, rows or [(self.look.t("no_activity"), "muted")])

    def text(self) -> str:
        """Everything shown (used by tests)."""
        return self.box.get("1.0", "end").strip()


class AuditView(Section):
    """Every safety decision, including denials with their reason. Collapsible."""

    def __init__(self, master: Any, look: Look, on_refresh: Callable[[], None] | None,
                 expanded: bool = False) -> None:
        super().__init__(master, look)
        self.expanded = expanded
        self.toggle_button = make_button(self, look, "", self.toggle, self.bg, width=220)
        self.toggle_button.grid(row=0, column=0, padx=10, pady=8, sticky="w")
        if on_refresh is not None:
            make_button(self, look, look.t("audit_refresh"), on_refresh, self.bg).grid(
                row=0, column=1, padx=10, pady=8, sticky="e")
        self.box = make_textbox(self, look, height=110, font=look.small)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._show()

    def toggle(self) -> None:
        """Show or hide the safety log."""
        self.expanded = not self.expanded
        self._show()

    def _show(self) -> None:
        key = "audit_hide" if self.expanded else "audit_show"
        self.toggle_button.configure(text=f"{'▾' if self.expanded else '▸'} {self.look.t(key)}")
        if self.expanded:
            self.box.grid(row=1, column=0, columnspan=2, padx=10, pady=(0, 10), sticky="nsew")
        else:
            self.box.grid_remove()

    def render(self, model: UiModel) -> None:
        """Draw the safety rows, newest first."""
        lang = self.look.lang
        rows = [(format_audit(e, lang) + "\n", audit_tag(e)) for e in reversed(model.audit)]
        write_lines(self.box, rows or [(self.look.t("no_audit"), "muted")])

    def text(self) -> str:
        """Everything shown (used by tests)."""
        return self.box.get("1.0", "end").strip()


@dataclass
class ControlCallbacks:
    """What the controls ask the app to do."""

    live: Callable[[bool], None]
    ptt_press: Callable[[], None]
    ptt_release: Callable[[], None]
    stop: Callable[[], None]
    wake_word: Callable[[str], None]
    language_hint: Callable[[str | None], None]
    settings: Callable[[], None]


class Controls(Section):
    """Live mode switch, hold-to-talk, Stop (F9), wake word on/off, language hint."""

    def __init__(self, master: Any, look: Look, callbacks: ControlCallbacks) -> None:
        super().__init__(master, look)
        pal, bg = look.palette, self.bg
        self.callbacks = callbacks
        self._ptt_down = False
        self._wake_word = ""

        self.live_switch = make_switch(self, look, look.t("live"), self._on_live, bg)
        self.live_switch.grid(row=0, column=0, padx=12, pady=10, sticky="w")
        self.ptt = make_button(self, look, look.t("hold_to_talk"), None, bg, width=220)
        self.ptt.grid(row=0, column=1, padx=8, pady=10)
        self.ptt.bind("<ButtonPress-1>", lambda _e: self.press_ptt())
        self.ptt.bind("<ButtonRelease-1>", lambda _e: self.release_ptt())
        _keys(self.ptt, ("<KeyPress-space>",), self.press_ptt)
        _keys(self.ptt, ("<KeyRelease-space>",), self.release_ptt)
        self.stop = make_button(self, look, look.t("stop"), callbacks.stop, bg, danger=True,
                                width=180)
        self.stop.grid(row=0, column=2, padx=8, pady=10)
        self.settings_button = make_button(self, look, look.t("settings"), callbacks.settings, bg)
        self.settings_button.grid(row=0, column=3, padx=8, pady=10, sticky="e")

        self.wake_switch = make_switch(self, look, look.t("wake_word"), self._on_wake, bg)
        self.wake_switch.grid(row=1, column=0, padx=12, pady=(0, 10), sticky="w")
        self.wake_entry = make_entry(self, look, look.t("wake_placeholder"), width=220)
        self.wake_entry.grid(row=1, column=1, padx=8, pady=(0, 10))
        self.wake_entry.bind("<Return>", lambda _e: self._on_wake_entry())
        lang_box = ctk.CTkFrame(self, fg_color="transparent")
        lang_box.grid(row=1, column=2, columnspan=2, padx=8, pady=(0, 10), sticky="e")
        ctk.CTkLabel(lang_box, text=look.t("language_hint"), font=look.base,
                     text_color=pal.text).grid(row=0, column=0, padx=(0, 8))
        self.language = make_menu(lang_box, look, hint_choices(look.lang), self._on_language, bg,
                                  width=240)
        self.language.grid(row=0, column=1)
        self.grid_columnconfigure(3, weight=1)

    # -- user input -----------------------------------------------------------------
    def _on_live(self) -> None:
        self.callbacks.live(bool(self.live_switch.get()))

    def press_ptt(self) -> None:
        """Start push-to-talk once (key auto-repeat is ignored)."""
        if not self._ptt_down:
            self._ptt_down = True
            self.callbacks.ptt_press()

    def release_ptt(self) -> None:
        """Stop push-to-talk if it was started."""
        if self._ptt_down:
            self._ptt_down = False
            self.callbacks.ptt_release()

    def _on_wake(self) -> None:
        word = self.wake_entry.get().strip() or self._wake_word
        if self.wake_switch.get() and not word:
            self.wake_switch.deselect()
            self.wake_entry.focus_set()
            return
        self.callbacks.wake_word(word if self.wake_switch.get() else "")

    def _on_wake_entry(self) -> None:
        word = self.wake_entry.get().strip()
        if word:
            self.wake_switch.select()
        else:
            self.wake_switch.deselect()
        self.callbacks.wake_word(word)

    def _on_language(self, choice: str) -> None:
        self.callbacks.language_hint(hint_code(choice, self.look.lang))

    # -- drawing --------------------------------------------------------------------
    def render(self, model: UiModel) -> None:
        """Match the switches and dropdown to the model."""
        if bool(self.live_switch.get()) != model.live:
            (self.live_switch.select if model.live else self.live_switch.deselect)()
        self._wake_word = model.wake_word
        if model.wake_word and not self.wake_entry.get().strip():
            self.wake_entry.insert(0, model.wake_word)
        (self.wake_switch.select if model.wake_word else self.wake_switch.deselect)()
        self.language.set(hint_choice(model.language_hint, self.look.lang))


class TypedInput(Section):
    """Type a command (or a yes / no answer) and press Enter."""

    def __init__(self, master: Any, look: Look, on_submit: Callable[[str], None]) -> None:
        super().__init__(master, look)
        self.on_submit = on_submit
        self.entry = make_entry(self, look, look.t("type_here"))
        self.entry.grid(row=0, column=0, padx=(12, 8), pady=10, sticky="ew")
        self.entry.bind("<Return>", lambda _e: self.send())
        self.entry.bind("<KP_Enter>", lambda _e: self.send())
        self.send_button = make_button(self, look, look.t("send"), self.send, self.bg, width=120)
        self.send_button.grid(row=0, column=1, padx=(0, 12), pady=10)
        self.grid_columnconfigure(0, weight=1)

    def send(self) -> None:
        """Submit the typed text and clear the box."""
        text = self.entry.get().strip()
        self.entry.delete(0, "end")
        if text:
            self.on_submit(text)

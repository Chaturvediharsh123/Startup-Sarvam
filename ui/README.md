# ui — the assistant window (WS7)

A simple, large, friendly customtkinter window that shows **what the assistant heard,
what it did, and how fast**. Built for a 65+ user reading from 1 m away: base text 18 pt
or more (default 20, headings x1.4), Nirmala UI for every Indian script, a high-contrast
theme, and every control reachable by keyboard.

| File | What it holds |
|---|---|
| `app.py` | `AssistantApp`, `SettingsDialog`, `run_app`, `format_action`, `format_timing` |
| `widgets.py` | `UiModel` (pure event -> state mapping) and the widgets that draw it |
| `theme.py` | `dark`, `light`, `high_contrast` palettes; font sizes |
| `labels.py` | every visible label in English and Hindi: `t(key, lang)`; language hint list |

## Parts of the window

Status bar (state in friendly words, mic level, language, one health dot per module:
audio, stt, brain, actions, safety, tts) · Controls (Live mode, Hold to talk, **Stop**,
Settings, wake word on/off + word, language hint) · Live transcript (partial in grey,
final in the strong text colour) · Reply panel (what it said / what it did) · Speed
(STT, LLM, first audio, total in ms) · Activity log (last 20 actions) · Safety log
(collapsible: every decision incl. denials and the reason) · Typed command box.

## Events in (bus -> window)

The window calls `bus.queue()` once and drains it every 50 ms with `after()` on the Tk
thread, so no other thread ever touches a widget.

| Event | Shown as |
|---|---|
| `StateChanged` | "Listening… / Thinking… / Speaking… / Waiting for your yes" |
| `HealthChanged` | dot colour + ✓ / ✗ per module (seeded from `orchestrator.health()`) |
| `PartialTranscript`, `FinalTranscript` | live transcript |
| `SpeakRequest` | "Assistant said" (what was actually spoken) |
| `BrainResult` | clears "did" for the new reply; updates the language |
| `ActionResult` | "Assistant did" + activity log line (time, name, status, fact) |
| `SafetyDecision` | safety log line; a denial is also shown under "did" |
| `Metrics` | speed readout |
| `Status`, `ErrorRaised`, `Interrupt` | detail line under the state (errors in red) |
| `LanguageDetected`, `MicLevel`, `ModeChanged` | language label, level meter, controls |

## Out (window -> orchestrator)

Only public orchestrator methods: `interrupt("user")` (Stop button, **F9** anywhere in the
app, **Esc**), `start_live()` / `stop_live()` (switch, **Ctrl+L**), `ptt_press()` /
`ptt_release()` (hold the button, Space on it, or **F8** in the window), `submit_typed()`
(Enter; also answers a pending yes/no), `set_wake_word()`, `set_language_hint()` (None =
auto). Calls that may block run in order on one worker thread; `interrupt` runs at once.

## Settings read and written

Reads `ui_font_size`, `ui_theme`, `ui_language`, `language_hint`, `wake_word`,
`tts_speaker`, `mic_device`. The settings dialog saves through
`save_settings({ENV_NAME: value})` (`MIC_DEVICE`, `SPEAKER_DEVICE`, `TTS_SPEAKER`,
`UI_FONT_SIZE`, `UI_THEME`, `UI_LANGUAGE`, `WAKE_WORD`, `LANGUAGE_HINT`), then applies
theme, text size and labels at once. Mic, speaker and voice apply after a restart.

## Run with fakes

```python
from core.bus import EventBus
from core.config import load_settings
from core.events import FinalTranscript
from ui.app import run_app

class FakeOrchestrator:
    live = False
    def start_live(self): return True
    def stop_live(self): ...
    def ptt_press(self): ...
    def ptt_release(self): ...
    def submit_typed(self, text): bus.publish(FinalTranscript(text))
    def interrupt(self, reason="user"): ...
    def set_language_hint(self, code): ...
    def set_wake_word(self, word): ...
    def health(self): return []

bus = EventBus()
run_app(FakeOrchestrator(), bus, load_settings(require_key=False))
```

Tests: `pytest tests/unit/ui` (pure model/labels/theme tests plus hidden-window smoke
tests that skip when Tk has no display).

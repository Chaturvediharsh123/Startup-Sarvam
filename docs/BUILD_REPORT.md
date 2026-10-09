# Build Report: Architecture Template Gaps

As of 2026-10-08 · branch `feature/pdf-architecture` (based on `main` at `b707dca`) · reference: "Voice PC Assistant – Architecture & Team Template" (Oct 5, 2026)

## Summary
The app now follows the PDF's architecture:
- A `core/` package with typed events, interfaces, a thread-safe event bus, a state machine and an orchestrator.
- One folder per workstream, each with a fake, start/stop/health and a README.
- `config/settings.yaml` and a Safety-owned `config/allowlist.yaml`.

Every gap in the earlier application review is closed or marked below as needing hardware or an API key. **826 tests pass** and `ruff check .` is clean. End-to-end runs of the real app passed in mock mode. Nothing was tested against the real Sarvam APIs, because no API key was available.

## How the code maps to the PDF
| PDF part | Where it lives now | Key pieces |
|---|---|---|
| Shared contracts | `core/events.py`, `core/interfaces.py`, `core/bus.py` | Every event carries `turn_id` + `ts`. Each module has a Protocol. The bus gives each subscriber its own queue, publishing never waits, and handler errors are logged. |
| Orchestrator + states | `core/orchestrator.py`, `core/state.py` | The PDF's six states plus Idle, with legal transitions enforced. One action per turn. Interrupt from any state. Errors become a spoken apology. |
| Config + logging | `core/config.py`, `config/settings.yaml`, `core/logging.py` | Precedence is settings.yaml < .env < environment. The API key is refused in YAML. Every log line carries the turn id, and keys are masked. |
| WS1 Audio | `audio/` | Mic auto-recovery, noise gate, resampler, speaker device choice, mute while speaking, stop in under 100 ms, `FakeMicrophone`/`FakeSpeaker`. |
| WS2 STT | `stt/` | Realtime Saaras with reconnect, push-to-talk fallback after N failures, local silence backup (900 ms), language hint, wake word, `FakeSTT`. |
| WS3 Brain | `brain/` | One LLM call, at most one tool call, prompt in the PDF's sections with few-shot examples in 7 languages, template result phrasing, memory v2, `FakeLLM`/`FakeBrain`. |
| WS4 Actions | `actions/` | Registry split into apps/web/system/typing_keys/notes, app aliases + Start Menu scan, friendly "not installed", 5 s timeout runner, short `fact`, `FakeActionRunner`. |
| WS5 Safety | `safety/`, `config/allowlist.yaml` | Guard with risk levels, multilingual yes/no, emergency-stop corner, JSONL audit log, 75-item red-team list. |
| WS6 TTS | `tts/` | Bulbul streaming with next-sentence prefetch, text cleaner (numbers, symbols), voice per language, cancel, `SilentTTS`/`MockTTS`. |
| WS7 UI | `ui/` | Health dots, grey partial / strong final text, activity log, safety log, speed readout, language hint, themes incl. high contrast, Hindi labels, keyboard-only use. |
| Testing + process | `tests/unit/<module>/`, `tests/integration/`, `tests/redteam/`, `tests/golden/`, `tools/golden_eval.py`, `.github/` | CI (ruff + pytest, Python 3.11/3.12), PR template with the definition of done, CODEOWNERS, ADRs 0001–0008. |

## Gaps closed
| Gap from the review | What was built | Where |
|---|---|---|
| Up to 5 actions per sentence | Brain keeps only the first PC tool call; orchestrator runs only `BrainResult.tool_call` | `brain/brain.py`, `core/orchestrator.py` |
| Interrupt only stopped audio | `CancelToken` per turn checked before the action and between sentences; Interrupt answers a pending yes/no with "no", stops TTS prefetch and returns to Listening | `core/orchestrator.py`, `core/interfaces.py` |
| No voice "stop" while busy | `is_stop_command` in 8 languages ("ruko", "bas", "रुको", "நிறுத்து", "ఆపు"…) interrupts at any time | `language/phrases.py` |
| Steps strictly one after another | Slow actions (>0.4 s) speak the brain's "doing it" sentence while running; templates replace the second LLM call | `core/orchestrator.py`, `brain/brain.py` |
| No filler, long timeouts | "ek second…" after `FILLER_AFTER_MS`; per-call LLM timeout; whole-turn watchdog `MAX_TURN_S` | `core/orchestrator.py`, `brain/llm_client.py` |
| TTS fetched sentence 2 only after 1 finished | Background prefetch of sentence N+1 into a bounded queue; closing the stream stops it | `tts/stream_client.py` |
| No turn id | `new_turn_id()` per sentence on every event, log line and audit entry | `core/events.py`, `core/logging.py` |
| No batch fallback, no local silence backup | "fallback" event after `STT_FALLBACK_AFTER` failures switches to push-to-talk; local silence detector | `stt/realtime_client.py`, `stt/vad.py` |
| No golden set, red-team, CI | 197 golden utterances + evaluator; 75 red-team items; GitHub Actions workflow | `tests/golden/`, `tools/`, `tests/redteam/`, `.github/` |
| Direct imports brain → safety → actions | Modules import only `core/` (and `language/`); `main.py` is the only place that wires concrete classes | all modules, `main.py` |
| No `core/` contracts | Created and covered by tests | `core/` |
| Allow-list in code | `config/allowlist.yaml` owned by Safety; both guard and actions read it | `config/`, `safety/allowlist.py` |
| No fakes, health, READMEs | Each module has `fake.py`, `health()`, `start()/stop()` and a one-page README | every module folder |
| Confirm 6 s, history 10 turns | 10 s and 4 turns (ADR 0005) | `config/settings.yaml` |
| No app aliases or Start Menu scan | Aliases in English, Devanagari and other scripts plus fuzzy matching; Start Menu scanned once at launch | `actions/apps.py`, `config/allowlist.yaml` |
| No per-action timeout | Thread-pool runner, status `timeout`, never raises | `actions/runner.py` |
| No "not installed" message | `NotInstalledError` → fact "photoshop not installed" → spoken in the user's language | `actions/`, `brain/brain.py` |
| Mic unplug ended live mode | Watchdog reopens the device every 2 s; health dot and error shown | `audio/mic.py` |
| No text cleaner or per-language voice | `clean_for_speech()`; `TTS_VOICES` | `tts/text_cleaner.py`, `config/settings.yaml` |
| UI missing health dots, language hint, themes, Hindi labels, audit view | All added | `ui/` |
| Two language detectors | `language/detect.py` is the single public API; V1 detector is its internal helper | `language/` |
| Repo hygiene | Removed committed `.pyc` files, the tracked empty `sarvam/.env`, empty `sarvam/main.py`, `requirments.txt`, and `sarvam/mock.py` (split into module fakes) | — |

## Extras added beyond the PDF
These were not asked for in the PDF. Each is small, covered by tests, and can be removed if the team disagrees.
1. **Thread-safe bus with a polling queue.** The UI drains a plain `queue.Queue` with Tk's `after()` instead of running a handler thread.
2. **Whole-turn watchdog** (`MAX_TURN_S`, 20 s), so a hung step can never freeze the assistant.
3. **Emergency stop stays on until the next command.** After the corner stop, actions are blocked until the user gives a fresh command.
4. **Secret masking in logs**, and refusing an API key placed in `settings.yaml`.
5. **`SPEAKER_DEVICE` setting**, so the speaker can be chosen as well as the mic.
6. **Memory schema v2**, adding `timeout` and `cancelled` action statuses, with an automatic migration.
7. **Extra Hinglish words for language detection**, e.g. "sab bhool jao", "ruko", "haan ji". These were previously detected as English.
8. **Photoshop entry in the allow-list.** It exists only so the PDF's "Photoshop installed nahi hai" example works.
9. **Mic resampling** when the device refuses 16 kHz.
10. **Three synthetic test WAVs** for the fake mic. Real recorded speech is still needed (PDF test level 4).
11. **Process files.** The PR template carries the PDF definition of done, plus "re-run the golden set" for brain or action changes.
12. **CODEOWNERS.** `@Chaturvediharsh123` owns `core/`; other owners are TODO placeholders.

## How this was tested
| Level (PDF) | What ran | Result |
|---|---|---|
| Unit | core 49 · audio 40 · stt 35 · brain 105 · actions 117 · safety 178 · tts 72 · ui 31 · language 85 · sarvam client 13 | pass |
| Contract | Orchestrator tests use stub modules written only against `core/interfaces.py`; the real modules pass the same flows in the integration tests | pass |
| Integration | 9 end-to-end tests through `main.build_app`: open app, "isko band karo" with confirmation, shutdown refused, volume, time, note saved, audit log with turn ids, health, the real window driving the real orchestrator, and a scripted `main.main(["--mock","--text"])` session | pass |
| Safety | 78 red-team tests: 69 denied, 3 need a spoken yes, 3 allowed and logged; no PC call made for any denied item | pass |
| Golden set | Schema tests (14) pass. Running `golden_eval.py --fake` shows the harness works. Its 52.8% comes from the keyword fake, not the real LLM. | harness OK; real accuracy not measured |
| Full suite | `pytest -q` run three times in a row | 826 passed each time |
| Real app | `python main.py --mock --text` with typed commands; `python main.py --mock` window started and ran for 12 s with no errors; `python main.py` without a key exits with a clear message | pass |

All PC side effects (processes, browser, volume, keys, lock, shutdown) are mocked in the tests. The manual mock-mode runs used only read-only actions (time, battery) and a shutdown that was refused, plus one note saved to a temp folder.

## Not verified here (needs a key, hardware or people)
- **Real Sarvam calls:** Saaras STT, sarvam-105b and Bulbul TTS. Run `python tools/golden_eval.py` and a live session once `SARVAM_API_KEY` is in `.env`.
- **The latency budget** (PDF total ~1.3–2.0 s). The speed readout now measures wall-clock time from end of speech to first sound, so it can be checked on a real run.
- **Real mic and speaker behaviour:** echo, a 30-minute capture run, and unplug/replug on real hardware.
- **Golden-set accuracy ≥ 85–90%**, per language, with the real LLM.
- **Recorded audio samples** per language (PDF test level 4), and user tests with elderly users (level 5).
- **Sandbox runs** of the risky actions (lock, shutdown, close app), which the PDF requires before merge.

## Known limitations
- **Threads, not asyncio.** The core uses threads plus a thread-safe bus, not a single asyncio loop (ADR 0001). The PDF lists asyncio as a principle, so this is a deliberate, documented deviation.
- **STT fallback needs the user.** When live STT is offline, the assistant asks the user to hold F8 and uses batch STT. It does not yet segment live audio into batch requests on its own.
- **No LLM token streaming yet.** Replies are short (≤ 2 sentences), and the filler covers slow calls.
- **Mic recovery can cut off a reply.** Recovery re-initialises PortAudio after a failed reopen, so a reply playing at that exact moment can be cut off.
- **Duplicate Memory/Action code.** The `memory_action` branch has its own Memory and Action blocks that duplicate `brain/memory.py` and `actions/`. The team should decide which to keep before merging both.

## Run it
```powershell
pip install -r requirements-dev.txt
python main.py --mock --text      # no key: typed commands, real safety and actions
python main.py --mock             # no key: the window
python main.py                    # with SARVAM_API_KEY in .env: live voice
python -m pytest -q; ruff check .
python tools/golden_eval.py       # real tool-choice accuracy (needs the key)
```

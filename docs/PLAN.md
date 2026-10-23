# Build plan: Startup-Sarvam voice PC assistant

> **Superseded.** This was the plan for the first build (single `assistant/` package). The code now follows the architecture template; see [BUILD_REPORT.md](BUILD_REPORT.md).

Status: built (phases 1–11). Real-API checks are pending until an API key is added; see README "Getting started".
API facts: see [sarvam_api_notes.md](sarvam_api_notes.md).

## 1. What exists today (inspected 2026-10-06, branch `feature/folders`)

| Path | State | Plan |
|---|---|---|
| `README.md` | Full product README (10 KB) | Keep. Rewrite setup, commands, safety and troubleshooting sections in Phase 11. |
| `language_spec.md` | V1 language spec (en / hi / hi-en) | Keep. `language/detect.py` follows it and adds the other scripts. |
| `LICENSE`, `.gitignore` | `.gitignore` has `.env`, `venv/`, `__pycache__/`, `logs/` | Extend `.gitignore` (see section 6). |
| `language/detector.py`, `normalizer.py`, `pipeline.py`, `types.py` + 4 test files | Your existing Hindi/English layer, with tests inside `language/` | **Keep untouched.** New `language/detect.py` reuses `detector.HINDI_WORDS` for Hinglish and adds multi-script detection. |
| `assistant/__init__.py`, `ui/__init__.py`, `tests/__init__.py` | Empty | Fill the packages. |
| `sandbox/.gitkeep`, `logs/` | Empty | Add the `.wsb` config and README. Keep `logs/` with a `.gitkeep`. |
| `sarvam/main.py` | **Empty, 0 bytes** | Entry point goes to root `main.py` (per brief). Ask before deleting this file. |
| `sarvam/.env` | **Empty, 0 bytes, and tracked by git** | `.env` moves to the project root. **No API key yet.** |
| `sarvam/requirments.txt` | Misspelled, uncommitted edits | New root `requirements.txt` replaces it. You can delete the old file. |

Python on this machine: 3.12.5 (`py -0` also lists 3.10). The venv will use 3.12.

## 2. Final folder tree

```
Startup-Sarvam/
  main.py                    # entry: python main.py [--text] [--ptt] [--no-ui]
  requirements.txt           # runtime deps, minimum versions pinned
  requirements-dev.txt       # pytest, pytest-mock, ruff
  pyproject.toml             # ruff + pytest config only (no packaging)
  .env.example               # placeholder settings
  .gitignore
  README.md  language_spec.md  LICENSE
  assistant/
    __init__.py
    config.py                # frozen Settings dataclass from .env; get_settings() cached
    logging_setup.py         # console + rotating logs/app.log; "actions" logger -> logs/actions.log
    memory.py                # SQLite memory: sessions, messages, actions, facts, schema_version
    actions.py               # @action registry, 16 allow-listed PC actions, tool_schemas()
    safety.py                # execute(): allow-list, arg validation, confirm, rate limit, log; is_yes(); failsafe
    brain.py                 # Brain.handle(): prompt, LLM tool loop, remember_fact, Reply dataclass
    pipeline.py              # real-time loop: mic -> STT -> brain -> TTS, event queue, timings
  sarvam/
    __init__.py
    client.py                # shared httpx.Client, auth header, timeouts, retry on 429/5xx, SarvamError
    stt_realtime.py          # asyncio WebSocket client for /speech-to-text-realtime/ws
    stt_rest.py              # POST /speech-to-text (saaras:v4, language_code=unknown)
    llm.py                   # POST /v1/chat/completions with tools
    tts.py                   # POST /text-to-speech/stream (linear16), REST fallback, sentence split
  language/
    __init__.py
    detector.py normalizer.py pipeline.py types.py test_*.py   # existing, unchanged
    detect.py                # Unicode script detection -> Sarvam code; prefer STT code; Odia or/od mapping
    phrases.py               # yes/no word lists (8 languages, native + romanised), canned replies
  audio/
    __init__.py
    mic.py                   # sounddevice InputStream 16 kHz int16 mono, 100 ms chunks -> queue, mute flag
    player.py                # OutputStream 24 kHz, play chunks as they arrive, stop(), is_speaking, unmute delay
    wakeword.py              # optional wake word: match romanised + native script, strip it
  ui/
    __init__.py
    app.py                   # customtkinter window; polls the event queue with after()
  sandbox/
    Startup-Sarvam.wsb       # Windows Sandbox config (placeholder host path)
    sandbox_setup.ps1        # runs inside the sandbox: installs Python + deps, starts the app
    README.md                # Windows Sandbox (Pro) + VirtualBox snapshot steps (Home)
  tests/
    __init__.py
    conftest.py              # in-memory settings, fake Memory, mocks for pycaw/pyautogui/psutil/os.startfile/subprocess, fake httpx + WS
    test_config.py test_memory.py test_actions.py test_safety.py
    test_brain.py test_language.py test_pipeline.py test_sarvam.py test_audio.py
    test_speech.py test_ui.py
  data/                      # assistant.db created at runtime (gitignored)
  logs/                      # app.log, actions.log (gitignored, .gitkeep kept)
  docs/
    sarvam_api_notes.md
    PLAN.md
```

## 3. Data flow

```
          +--------------------------- live mode ----------------------------+
mic.py ---| 100 ms int16 chunks (dropped while muted) --> stt_realtime.py (WS) |
          |   <-- transcript.partial --> event "partial" --> UI (grey text)   |
          |   <-- transcript.final(text, language) after server VAD           |
          +--------------------------------------------------------------------+
                 |  (push-to-talk: record while F8/button held -> stt_rest.py)
                 |  (--text: typed line, language from detect.py)
                 v
         wakeword.py (optional strip/ignore)
                 v
   Brain.handle(text, language, ask_user)         [worker thread]
     messages = system prompt + memory.context_note() + last N turns + user
     llm.chat(messages, tools=tool_schemas() + remember_fact)
       |-- no tool calls --------------------------> reply text
       |-- tool calls --> safety.execute(name, args, memory, ask_user)
       |                    allow-list -> validate args -> rate limit
       |                    -> risky? ask_user("PC band karoon? haan ya nahi") -> is_yes()
       |                    -> run action (timed) -> memory.log_action + logs/actions.log
       |-- informational tool results --> 2nd llm call --> reply in user language
       |-- normal actions --> 1st call text, or phrases.py confirmation
       |-- any denied/blocked/error --> reply says so (never fake "done")
     memory.add_message(user), memory.add_message(assistant)
                 v
   tts.py: split into sentences -> POST /text-to-speech/stream per sentence
                 v
   player.py: plays PCM as it arrives; sets mic mute; unmutes 300 ms after end
                 v
   events: reply, action, timing (STT, LLM, action, TTS first audio, total) -> UI
                 v
   listen again
```

Threads:
- **Tk main thread:** UI only. Polls `events` (a `queue.Queue`) every 50 ms with `after()`.
- **STT thread:** its own asyncio loop for the WebSocket. Pulls chunks from the mic queue and pushes finals into the turn queue.
- **Brain worker thread:** one turn at a time. Runs Brain, safety, actions and TTS.
- **Audio callbacks:** sounddevice threads. They only touch queues and flags.

Every thread has a stop `Event` and a `join(timeout)` in `Pipeline.stop()`. Stopping live mode sends `{"event":"end"}` and closes the WebSocket, so no credits are spent.

Voice `ask_user`: speak the question, then wait for **one** `transcript.final` for up to `confirm_timeout_s` (6 s). A timeout returns `""`, which counts as no. The brain worker blocks on a dedicated "confirm" queue. The STT thread routes the next final there while a confirmation is pending.

## 4. Actions (16)

| # | Action | Risky | Informational | Notes |
|---|---|---|---|---|
| 1 | `open_app(name)` | | | enum from `APPS`; `os.startfile` or `subprocess.Popen([...])`, never `shell=True` |
| 2 | `close_app(name)` | yes | | enum from `PROCESS_NAMES`; psutil terminate, then kill after a timeout |
| 3 | `web_search(query)` | | | Google URL, `quote_plus`, `webbrowser.open` |
| 4 | `youtube_search(query)` | | | YouTube results URL |
| 5 | `set_volume(level)` | | | integer 0–100, clamped; pycaw new (`AudioUtilities.GetSpeakers().EndpointVolume`) and old (`Activate`) APIs |
| 6 | `change_volume(direction, step=10)` | | | up/down/mute/unmute |
| 7 | `type_text(text)` | | | clipboard + Ctrl+V, restore old clipboard, max 2000 characters |
| 8 | `press_shortcut(shortcut)` | | | enum of `SAFE_HOTKEYS` only |
| 9 | `scroll(direction, amount)` | | | small/medium/large |
| 10 | `take_screenshot()` | | | PNG in `notes_dir`, timestamped |
| 11 | `system_status()` | | yes | battery, charging, CPU, RAM, disk |
| 12 | `lock_pc()` | yes | | `ctypes.windll.user32.LockWorkStation()` |
| 13 | `shutdown_pc()` | yes | | `["shutdown", "/s", "/t", str(delay)]`; the reply says how to cancel |
| 14 | `cancel_shutdown()` | | | `["shutdown", "/a"]` |
| 15 | `save_note(title, content)` | | | sanitised filename, UTF-8, opened after save, returns path |
| 16 | `current_time()` | | yes | local date and time |

The brain also has `remember_fact(key, value)`. It does not touch the PC, so it lives in the brain, not in `ACTIONS`.

## 5. Settings (`.env` → `Settings`)

`SARVAM_API_KEY` (required) · `SARVAM_STT_REALTIME_MODEL=saaras:v3-realtime` · `SARVAM_STT_MODEL=saaras:v4` · `SARVAM_LLM_MODEL=sarvam-105b` · `SARVAM_LLM_REASONING=low` · `SARVAM_TTS_MODEL=bulbul:v3` · `TTS_SPEAKER=priya` · `TTS_SAMPLE_RATE=24000` · `DEFAULT_LANGUAGE=hi-IN` · `SAMPLE_RATE=16000` · `CHUNK_MS=100` · `VAD_SILENCE_MS=500` · `HISTORY_TURNS=10` · `WAKE_WORD=` · `CONFIRM_TIMEOUT_S=6` · `DB_PATH=data/assistant.db` · `LOG_DIR=logs` · `SHUTDOWN_DELAY_S=60` · `NOTES_DIR=` (empty means `~/Documents/Sarvam Assistant`) · `MIC_DEVICE=` · `MAX_ACTIONS_PER_MINUTE=10` · `BARGE_IN=false`

Relative paths resolve against the project root, not the current working directory.

## 6. Dependencies

`requirements.txt`: httpx, websockets, python-dotenv, sounddevice, numpy, scipy, pyautogui, pillow (pyautogui screenshots need it), pyperclip, psutil, pycaw, comtypes, customtkinter, keyboard.
`requirements-dev.txt`: `-r requirements.txt`, pytest, pytest-mock, ruff.
`.gitignore` adds: `.env` (already there), `.venv/`, `data/`, `logs/*.log`, `__pycache__/`, `*.db`, `.pytest_cache/`, `.ruff_cache/`.

## 7. Differences from BUILD_PROMPT.md

1. **HTTP library:** `httpx` instead of `requests`, and no `sarvamai` SDK (see the notes file for why).
2. **TTS:** HTTP streaming endpoint, not the TTS WebSocket. `linear16` at 24 kHz, so no MP3 decoding.
3. **End of speech:** server VAD events. No local energy detector.
4. **LLM:** `sarvam-105b` by default, configurable. The docs recommend `sarvam-105b-conversations` for voice. We can switch if latency is high.
5. **Language package:** I add `detect.py` and `phrases.py` next to your existing `detector.py` and friends instead of replacing them. Your 4 test files stay in `language/` and still run.
6. **Extra test files:** `test_sarvam.py` (HTTP/WS clients with mocks) and `test_audio.py` (mute flag, wake word).
7. **`pyproject.toml`:** added only for ruff and pytest config.
8. **Push-to-talk key:** F8 (F9 is "stop speaking").

## 8. What I need from you

1. **API key:** `sarvam/.env` is empty. Before Phase 5 (text mode against the real API), put `SARVAM_API_KEY=sk_...` in a root `.env`. I will create `.env.example`.
2. **Old files:** may I delete the empty `sarvam/main.py` and empty `sarvam/.env`? `sarvam/.env` is tracked in git, so it also needs `git rm --cached`. It is empty, so no secret was leaked. Otherwise I leave both alone.
3. **Old requirements:** after Phase 2 you can delete `sarvam/requirments.txt`.
4. **Branch:** I build on the current branch `feature/folders`. No commits unless you ask.

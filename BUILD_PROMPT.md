You are building a project from scratch in this folder: a real-time, voice-controlled Windows PC assistant for Indian users, built entirely on Sarvam AI (speech-to-text, LLM, text-to-speech). Work carefully and in depth. Write proper production-quality Python, check every file you write, and test as you go.

# 0. Ground rules (read first, follow always)

- OS is Windows 10/11, shell is PowerShell, Python 3.11+. Use a virtual env: `python -m venv .venv` then `.venv\Scripts\activate`.
- FIRST inspect the folder before writing anything: list every folder and file, read `README.md`, `language_spec.md`, `main.py`, `.gitignore`, `.env` (only check which keys exist, NEVER print the key values), and anything inside `assistant/`, `language/`, `logs/`, `sandbox/`, `sarvam/`, `tests/`, `ui/`. Reuse this existing folder layout. Do not delete or overwrite any non-empty file the user wrote without asking me first.
- There is a misspelled `sarvam/requirments.txt`. Create a correct `requirements.txt` in the project root and tell me so I can delete the old one.
- NEVER guess Sarvam API details. Before writing any code that calls Sarvam, open the official docs with your web fetch tool and confirm the exact endpoint URL, auth header, request fields, response fields, model names, WebSocket message format, audio format (sample rate, encoding) and language codes. Start at https://docs.sarvam.ai and find the pages for: Speech-to-Text (REST), Speech-to-Text streaming / realtime (WebSocket), Chat Completions (and its tool/function-calling support), Text-to-Speech, and Text-to-Speech streaming. Write what you confirmed into `docs/sarvam_api_notes.md` with the source URL for each fact. If the docs and this prompt disagree, the docs win; tell me what was different.
- The models the project brief names are: `saaras:v3-realtime` (live STT over WebSocket), `saaras:v4` (push-to-talk STT, auto language detect), `sarvam-105b` (LLM with function calling), `bulbul:v3` (streaming TTS). Verify these exact names exist in the docs. If a name is wrong, use the closest real one and tell me.
- If the Sarvam Python SDK (`sarvamai` on PyPI) supports what we need, you may use it; otherwise use `requests`/`httpx` and `websockets`. Pick one and stay consistent.
- Secrets: the API key lives only in `.env` as `SARVAM_API_KEY`. Create `.env.example` with placeholder values. Make sure `.gitignore` covers `.env`, `.venv/`, `data/`, `logs/*.log`, `__pycache__/`, `*.db`.
- Every module: type hints, docstrings, the `logging` module (no bare `print` except in CLI tools), clear error handling. No function longer than ~50 lines. No hard-coded paths; everything comes from config.
- Windows-only imports (`pycaw`, `comtypes`, `ctypes.windll`, `pyautogui`) go INSIDE the functions that need them, so the test suite runs anywhere.
- Tests must NEVER actually shut down, lock, close apps, type, or change volume on my PC. Mock all of that.
- Use a todo list. Work phase by phase (below). At the end of each phase: run `python -m pytest -q`, run `ruff check .` (install ruff as a dev dependency), fix everything, then print a short summary of the files created/changed and how to try that phase by hand.
- After Phase 1 (plan), STOP and wait for me to say "go". After that, continue through all phases without stopping, unless you hit something only I can do (missing API key, microphone permission, a Sarvam docs question you can't answer). If blocked, explain exactly what you need.

# 1. Phase 1 — inspect and plan

Inspect the folder (rule above), read the Sarvam docs, write `docs/sarvam_api_notes.md`, then write `docs/PLAN.md`: the final folder tree, what each file does, the data flow, and any differences from this prompt. Show me the plan and wait.

# 2. Target architecture

One spoken sentence becomes an action and a spoken reply:

```
mic (live) -> Saaras realtime STT (detects pause) -> Brain (sarvam-105b + tools + memory)
   -> Safety check (allow-list, spoken "haan?" for risky) -> PC action
   -> Bulbul streaming TTS (starts speaking before full audio arrives) -> listen again
Questions with no action skip straight to the spoken answer.
```

Target folder layout (adapt to what already exists):

```
Startup-Sarvam/
  main.py                    # entry point: python main.py [--text] [--ptt] [--no-ui]
  requirements.txt
  requirements-dev.txt       # pytest, pytest-mock, ruff
  .env.example
  assistant/
    __init__.py
    config.py                # settings loaded from .env, one Settings dataclass
    logging_setup.py         # console + rotating file log in logs/
    memory.py                # SQLite conversation memory
    actions.py               # the allow-listed PC actions (@action registry)
    safety.py                # allow-list + confirmation + action log + failsafe
    brain.py                 # LLM call, tool calling, reply building
    pipeline.py              # the real-time loop that connects everything
  sarvam/
    __init__.py
    client.py                # shared HTTP session, auth header, retries, timeouts
    stt_realtime.py          # WebSocket live STT
    stt_rest.py              # push-to-talk / fallback STT
    llm.py                   # chat completions with tools
    tts.py                   # streaming TTS
  language/
    __init__.py
    detect.py                # script detection (Devanagari/Tamil/Telugu/... / Latin)
    phrases.py               # yes/no words, canned replies per language
  audio/  (or inside assistant/ if you prefer)
    mic.py                   # sounddevice input stream, 100 ms chunks, mute flag
    player.py                # streaming playback, interrupt/stop
    wakeword.py              # optional text-based wake word check
  ui/
    __init__.py
    app.py                   # customtkinter window
  sandbox/
    Startup-Sarvam.wsb       # Windows Sandbox config
    README.md                # how to use Windows Sandbox and VirtualBox snapshots
  tests/
    conftest.py              # fixtures, mocks for Windows libs and Sarvam HTTP/WS
    test_config.py
    test_memory.py
    test_actions.py
    test_safety.py
    test_brain.py
    test_language.py
    test_pipeline.py
  data/                      # SQLite db is created here at runtime (gitignored)
  logs/                      # actions.log, app.log (gitignored)
  docs/
```

# 3. Phase 2 — config, logging, skeleton

- `config.py`: a frozen `Settings` dataclass loaded with `python-dotenv`. Fields: `sarvam_api_key` (required, clear error if missing), model names for each Sarvam model, `default_language` (e.g. `hi-IN`), `tts_speaker`, `sample_rate` (whatever the docs require, likely 16000), `chunk_ms=100`, `history_turns=10`, `wake_word` (optional, empty = off), `confirm_timeout_s=6`, `db_path=data/assistant.db`, `log_dir=logs`, `shutdown_delay_s=60`, `notes_dir` (Documents\Sarvam Assistant). A `get_settings()` function caches it.
- `logging_setup.py`: console handler + `RotatingFileHandler` for `logs/app.log`; a separate logger `actions` writing `logs/actions.log`.
- `requirements.txt` with pinned minimum versions: requests or httpx, websockets, python-dotenv, sounddevice, numpy, scipy, pyautogui, pyperclip, psutil, pycaw, comtypes, customtkinter, keyboard (for F9 hotkey). Install them and confirm imports work on this machine.
- `main.py` with argparse: `--text` (type instead of speak), `--ptt` (push-to-talk instead of live), `--no-ui` (terminal only). Initially it can just load settings and print "ok".

# 4. Phase 3 — conversation memory (SQLite)

`assistant/memory.py`, class `Memory`, standard-library `sqlite3`, thread-safe (`check_same_thread=False` + a `threading.Lock` around every query), WAL mode on, foreign keys on. Accept `":memory:"` for tests.

Tables:
- `sessions(id, started_at, ended_at, language)`
- `messages(id, session_id, role CHECK IN ('user','assistant'), content, language, created_at)` + index on (session_id, id)
- `actions(id, session_id, name, args JSON, result, status CHECK IN ('ok','error','denied','blocked'), duration_ms, created_at)` + index
- `facts(key PRIMARY KEY, value, updated_at)` — long-term memory like name, city, preferred language
- `schema_version(version)` with a tiny migration function so the schema can change later without losing data.

Methods: `add_message`, `recent_messages(limit, all_sessions=False)` (oldest first, ready for the LLM), `search_messages(text, limit)`, `log_action`, `last_action()` (only status ok — this is how "ab isko band karo" knows what "isko" means), `remember(key, value)` (keys normalised lowercase), `recall`, `all_facts`, `forget(key)`, `context_note()` (short text for the LLM: known facts + last action), `clear_session()` ("sab bhool jao"), `end_session()`, `close()`. Make it a context manager.

Tests: ordering, limit, cross-session search, last_action ignores denied/error, facts upsert and case-insensitive keys, Unicode (Hindi/Tamil) round-trips, concurrent writes from 5 threads don't crash, migration on an existing db.

# 5. Phase 4 — actions and safety

`assistant/actions.py`: an `@action(description, params, required, risky=False, informational=False)` decorator that registers each function in an `ACTIONS` dict and builds an OpenAI-style tool schema (`{"type":"function","function":{name, description, parameters}}`) — confirm in the Sarvam docs that this is the tool format they accept. `tool_schemas()` returns the list. Use JSON-schema `enum`s wherever the choices are fixed so the LLM can't invent values. Every action returns a short English result string (or a small dict for informational ones).

The 15 actions:
1. `open_app(name)` — enum from an `APPS` map (notepad, chrome, edge, calculator, paint, explorer, word, excel, settings, file manager...). Launch with `os.startfile` or `subprocess` without `shell=True` and without building command strings from LLM input.
2. `close_app(name)` — risky; psutil terminate only processes in a `PROCESS_NAMES` map; graceful terminate then kill after timeout.
3. `web_search(query)` — Google results URL, `urllib.parse.quote_plus`.
4. `youtube_search(query)` — YouTube results URL.
5. `set_volume(level 0-100)` — pycaw; handle both new and old pycaw APIs; clamp value.
6. `change_volume(direction up/down/mute/unmute, step=10)`.
7. `type_text(text)` — must work for Hindi/Tamil/etc: copy to clipboard with pyperclip, Ctrl+V, restore old clipboard. Max length limit.
8. `press_shortcut(shortcut)` — enum of a SAFE_HOTKEYS map only (save, copy, paste, undo, redo, select_all, new_tab, close_tab, refresh, enter, minimize_all, switch_window, zoom_in, zoom_out).
9. `scroll(direction up/down, amount small/medium/large)`.
10. `take_screenshot()` — saves PNG to notes_dir with timestamp.
11. `system_status()` — informational: battery, charging, CPU, RAM, disk.
12. `lock_pc()` — risky.
13. `shutdown_pc()` — risky; `shutdown /s /t <shutdown_delay_s>`; tells the user how to cancel.
14. `cancel_shutdown()`.
15. `save_note(title, content)` — the LLM writes the full letter/note itself; sanitise filename, write UTF-8, open it, return the path. Also `current_time()` informational. If this makes more than 15, that's fine; list them all in the README.

`assistant/safety.py`:
- `execute(name, args, memory, ask_user)` is the ONLY way any action runs. Order: allow-list check (unknown -> `blocked`), validate args against the schema (types, enums, ranges, required — reject extra keys; -> `error`), if risky call `ask_user(question_in_users_language)` and require a clear yes (-> `denied` otherwise), run with try/except and timing, log to memory + `logs/actions.log`, return the result.
- `is_yes(text)`: True ONLY for a clear yes in Hindi/Hinglish/English/Tamil/Telugu/Kannada/Bengali/Marathi (both native script and romanised). Any "no" word anywhere, silence, None, noise, or a question = False. Put the word lists in `language/phrases.py`.
- `enable_failsafe()`: pyautogui FAILSAFE on (mouse to a screen corner stops automation), small PAUSE.
- A rate limit: max N actions per minute so a looping LLM can't spam the PC.

Tests: every action's schema is valid, enums match the maps, unknown action blocked + logged, bad args rejected, risky action with "nahi"/silence/"haan nahi"/no ask_user is denied and the function is NEVER called (assert with a mock), "haan"/"हाँ"/"yes"/"சரி" passes, exceptions become `error` results, rate limit works, filename sanitising blocks `..\` and reserved names.

# 6. Phase 5 — the brain + text mode

`sarvam/llm.py`: one function to call Chat Completions with messages + tools, timeout, 2 retries with backoff on 429/5xx only, returns the assistant message. Streaming optional.

`assistant/brain.py`, class `Brain(memory, llm, settings)`, method `handle(user_text, language, ask_user) -> Reply` (dataclass: text to speak, language, actions run, timings in ms):
- Build messages: system prompt + `memory.context_note()` + last N turns + new user message, and pass `tool_schemas()` plus a `remember_fact(key, value)` tool handled inside the brain.
- System prompt rules: reply in the SAME language AND script the user used (Hindi in Devanagari, Hinglish in Roman, Tamil in Tamil script...); 1–2 short sentences because it is spoken; call a tool when the user wants something done on the PC, only the given tools; "isko/usko/it/that" refers to the last action; save personal facts with remember_fact; for a leave application/letter, write the complete text into save_note; if unsure, ask a short question instead of guessing.
- Run each tool call through `safety.execute`. For informational actions (system_status, current_time) send the tool results back to the LLM in a second call so it answers in the user's language; for normal actions use the first call's text, or a short localised confirmation from `language/phrases.py` if the text is empty.
- If any action was denied/blocked/errored, the spoken reply must say so (no fake "done").
- If the LLM call fails: a localised "sorry, please say it again" reply, logged, never crash.
- Save user and assistant turns to memory.

`language/detect.py`: detect the script of a text by Unicode ranges (Devanagari, Bengali, Gurmukhi, Gujarati, Oriya, Tamil, Telugu, Kannada, Malayalam, Latin) and map to a Sarvam language code; romanised Hindi defaults to `hi-IN` (Hinglish). Prefer the language code STT returns when available.

Text mode: `python main.py --text` — type a command, see the reply, risky actions ask for confirmation in the terminal. This must work end-to-end with the real API before moving on. Tests mock the LLM: a tool call runs the action, denied action changes the reply, remember_fact stores a fact, history is passed, LLM error gives the fallback reply.

# 7. Phase 6 — voice output (Bulbul TTS)

`sarvam/tts.py`: streaming TTS per the docs (HTTP stream or WebSocket — whichever the docs describe for bulbul:v3); yield audio chunks as they arrive; decode to PCM (base64/wav/mp3 per docs). Fallback to non-streaming TTS if streaming fails. Split long text into sentences so speech starts fast.

`audio/player.py`: plays chunks with a `sounddevice.OutputStream` as they arrive; `stop()` interrupts instantly; exposes `is_speaking`. While speaking, set the shared mic-mute flag so the assistant doesn't hear itself; unmute ~300 ms after playback ends. F9 (via `keyboard`) and the UI button call `stop()`.

# 8. Phase 7 — voice input (Saaras STT)

`audio/mic.py`: `sounddevice.InputStream` at the sample rate the docs require, mono, int16, 100 ms chunks into a `queue.Queue`; drops chunks while muted; device selection from config.

`sarvam/stt_realtime.py`: asyncio WebSocket client per the docs: connect with auth, send config, stream base64/PCM chunks, receive partial and final transcripts plus the language code, detect end of speech (use the server's VAD/end-of-utterance event if the docs provide one; otherwise local energy-based silence detection ~700 ms). Callbacks `on_partial(text)` and `on_final(text, language)`. Auto-reconnect with backoff; keepalive; clean shutdown.

`sarvam/stt_rest.py`: push-to-talk fallback — record while a key/button is held, send WAV to the REST endpoint with saaras:v4 and language auto-detect.

`audio/wakeword.py`: optional; if `wake_word` is set, only act on finals that start with it (match romanised and native script, e.g. "sarvam"/"सर्वम"), and strip it.

`ask_user` for voice mode: speak the confirmation question, listen for ONE final transcript with `confirm_timeout_s` timeout, return it (timeout -> empty string -> counts as no).

# 9. Phase 8 — the real-time pipeline

`assistant/pipeline.py`: wires mic -> STT -> brain -> safety -> TTS in background threads (asyncio loop in its own thread for the WebSocket; brain runs in a worker thread so the UI never freezes). Events go to the UI through a thread-safe queue: `partial`, `final`, `reply`, `action`, `status`, `timing`, `error`. Measure and emit timings for each stage: STT final, LLM, action, TTS first audio, total. One turn at a time: new speech while speaking = interrupt the reply (barge-in) only if the user enables it. Live mode can be started/stopped; when stopped, close the WebSocket so no Sarvam credits are spent. Run `--no-ui` mode in the terminal too.

Tests: with fake STT/LLM/TTS, a final transcript produces a reply event, mic is muted during speech, stop() closes the connection, an exception in one turn doesn't kill the loop.

# 10. Phase 9 — the window (customtkinter)

`ui/app.py`: dark theme, large readable fonts (for elderly users), Devanagari/Tamil-capable font (Nirmala UI on Windows). Contents: a big Live mode on/off switch, a mic level meter, the live transcript (partials in grey, finals in white), the assistant reply, an activity log of actions with ✓/✗/denied, a speed readout after every reply (e.g. "STT 0.4s · LLM 0.9s · voice 0.3s"), a Stop speaking button (F9), push-to-talk button, language indicator, and a settings dialog for speaker voice and wake word. Update widgets only from the Tk main thread by polling the event queue with `after()`. Closing the window shuts down threads, the WebSocket and the db cleanly. `python main.py` starts the UI.

# 11. Phase 10 — sandbox

`sandbox/Startup-Sarvam.wsb`: Windows Sandbox config that maps the project folder (use a placeholder path and explain how to edit it), enables audio input and networking, and runs a logon command that installs Python deps and starts the app (or opens a terminal there). `sandbox/README.md`: how to enable Windows Sandbox (Pro only), how to use it, and the VirtualBox alternative for Windows Home with snapshot/rollback steps.

# 12. Phase 11 — final review (do not skip)

- Re-open and read EVERY file you created, top to bottom. Check: imports used and correct, no leftover TODOs, no secrets, no `shell=True`, no command strings built from LLM output, every Sarvam call matches `docs/sarvam_api_notes.md`, every thread has a clean shutdown, every file has docstrings and type hints.
- Run `ruff check .` and `python -m pytest -q` — all green.
- Run `python main.py --text` and try: "Notepad kholo", "Volume 30 kar do", "mera naam Harsh hai", "mera naam kya hai?", "Battery kitni hai?", "Chhutti ki application likho", "PC band karo" then answer "nahi". Show me the output.
- Update `README.md`: what it is, setup (venv, `.env`, install), how to run each mode, the list of voice commands per action, the safety model, how to add a new action (one function + decorator), the database tables and how to inspect `data/assistant.db`, troubleshooting (mic not found, API key, pycaw errors, Hindi typing).
- Finish with: the final folder tree, a table of every file and its purpose, the test results, and anything you could not verify or that I must check by hand (mic, speakers, real voice latency).

Do not commit or push to git unless I ask. Start with Phase 1 now.

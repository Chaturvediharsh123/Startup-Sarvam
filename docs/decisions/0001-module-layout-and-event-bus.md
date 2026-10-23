# 0001. Module layout and event bus

- Status: Accepted
- Date: 2026-10-08

## Context

Six workstreams (audio/STT, brain/memory, actions, safety, TTS, UI) build in parallel.
They need clear boundaries so one person's change does not break another's, and fakes
must be swappable for real modules in tests. The STT client already needs its own
asyncio loop for the WebSocket, but most libraries we call (pyautogui, pycaw, sounddevice,
Tk) are blocking and thread-based.

## Decision

- One top-level package per workstream: `audio/`, `stt/`, `brain/`, `language/`,
  `actions/`, `safety/`, `tts/`, `ui/`, plus `sarvam/` (shared HTTP client). Shared
  contracts live in `core/` (`events.py`, `interfaces.py`, `bus.py`, `config.py`,
  `orchestrator.py`).
- Modules talk only through the dataclass events in `core/events.py` and the Protocols in
  `core/interfaces.py`. Modules import from `core`, never from each other.
- Every long-lived module follows the module template: `start()`, `stop()`, `health()`.
- The event bus (`core/bus.py`) is **thread-based and thread-safe** now: each subscriber
  has its own queue and dispatcher thread; `publish` never blocks; a failing handler is
  logged, not fatal. The UI drains a plain `queue.Queue` with Tk `after()`.
- Only the realtime STT client runs an asyncio loop, on its own thread.
- Moving the whole app to asyncio is deferred until profiling shows that thread overhead
  matters.

## Consequences

- Fakes implement the same Protocol, so unit tests run without a microphone, network or
  Windows APIs.
- `core/` is a frozen contract: changes need the tech lead's review (`.github/CODEOWNERS`).
- Threads mean shared state (memory DB, settings) must be locked; `brain.memory.Memory`
  already serialises access on one connection.
- A later asyncio migration only has to replace `core/bus.py` and the orchestrator's
  worker threads, not the module interfaces.

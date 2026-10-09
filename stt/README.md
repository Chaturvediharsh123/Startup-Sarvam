# WS2 Speech-to-text (`stt/`)

Mic audio in, partial and final transcripts out. Imports only `core/`, `language/` and `sarvam/client.py`.

| File | What it does |
|---|---|
| `realtime_client.py` | `RealtimeSTT`: Saaras realtime over WebSocket (implements `SpeechToText`). Server VAD end-of-speech, keep-alive pings, auto-reconnect with backoff, `"fallback"` event, local silence backup, language hint. |
| `batch_client.py` | `RestSTT.transcribe(pcm, rate) -> (text, language)` (`BatchSpeechToText`): push-to-talk and fallback, auto language detection (`unknown`) or the hint. |
| `vad.py` | `LocalSilenceDetector`: if a partial was seen and then `local_silence_ms` of quiet audio passes with no final, the last partial becomes the final (once). |
| `wake_word.py` | `strip_wake_word(text, wake_word)`: optional text-level wake word ("hey sarvam ..."). Works on the whole transcript, so the word is never cut off. |
| `fake.py` | `FakeSTT` (scripted finals with partials) and `FakeBatchSTT` (scripted results). |

## Behaviour

- **End of speech:** Sarvam's `transcript.final` (server VAD, `vad_silence_ms`) is the normal signal. The
  local detector (700-900 ms, `local_silence_ms`) is the backup; a late server final for an utterance that
  was already finished locally is dropped, so a sentence is never delivered twice.
- **Reliability:** reconnect forever with 1 s .. 15 s backoff; after `stt_fallback_after` consecutive failed
  attempts emit `"fallback"` once (switch to push-to-talk with `RestSTT`) and keep retrying in the
  background; `"connected"` means live mode works again. Bad key / quota (`FatalSTTError`) emits
  `"error:<msg>"` and stops. Setting the `stop` event sends `end` and closes the socket (no credits used
  while live mode is off).
- **Language hint:** `RealtimeSTT.language_hint` / `RestSTT.language_hint` (start as `settings.language_hint`)
  are sent instead of `auto`/`unknown`; changes apply on the next connection / request. Finals without a
  detected language report the hint.

## Events (callbacks; the orchestrator turns them into bus events)

| Callback | Bus event |
|---|---|
| `on_partial(text)` | `PartialTranscript` |
| `on_final(text, language)` | `FinalTranscript` (starts a turn) |
| `on_event("speech_start")` | barge-in: `Interrupt` while speaking (if `barge_in`) |
| `on_event("speech_end" / "local_end_of_speech")` | end-of-speech timestamp for `Metrics` |
| `on_event("connected" / "disconnected" / "fallback" / "error:...")` | `HealthChanged`, `Status`, `ErrorRaised`; `fallback` = enable push-to-talk |

In: `ModeChanged(live=...)` → start/stop `run_blocking` (set the stop event); `ModeChanged(language_hint=...)`
→ set `language_hint` on both clients.

## Settings read

`stt_realtime_model`, `stt_model`, `sample_rate`, `vad_silence_ms`, `local_silence_ms`, `stt_fallback_after`,
`language_hint`, `noise_gate` (silence threshold of the local detector), `wake_word` (orchestrator).

## Fakes

```python
from stt.fake import FakeSTT, FakeBatchSTT
stt_factory = FakeSTT.factory([("notepad kholo", "hi-IN"), ("volume 40 karo", "hi-IN")], delay_s=0.2)
stt = stt_factory(on_partial, on_final, on_event); stt.run_blocking(mic.read, stop_event)
batch = FakeBatchSTT([("haan", "hi-IN")])
```

Tests (no network, fake WebSockets and `httpx.MockTransport`): `pytest tests/unit/stt`.

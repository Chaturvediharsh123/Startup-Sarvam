# WS6 Text-to-speech (`tts/`)

Reply text in, 16-bit mono PCM chunks out (`TextToSpeech`). Imports only `core/`, `language/` and
`sarvam/client.py`.

| File | What it does |
|---|---|
| `stream_client.py` | `SarvamTTS.stream(text, language) -> SpeechStream`: Bulbul `/text-to-speech/stream` (`linear16`), one request per sentence, REST fallback per sentence, voice per language, `cancel()`. |
| `sentence_splitter.py` | `split_sentences(text)`: sentences at `. ! ? । ॥` / newlines; short pieces merged, long ones cut at a comma or space (max 450 chars). Also importable from `stream_client`. |
| `text_cleaner.py` | `clean_for_speech(text, language)`: drops markdown, code, URLs, emoji; expands `% ₹ Rs $ °C °F & + = @`; numbers, decimals and times (`10:30`) to words in Devanagari Hindi, romanised Hindi or English (lakh/crore); known English words and acronyms inside Devanagari Hindi written in Devanagari (`PC` → `पीसी`). Other languages keep digits. |
| `fake.py` | `MockTTS` (prints / offline pyttsx3 voice, no PCM) and `SilentTTS` (silence with realistic duration). |

## Behaviour

- **First chunk ASAP:** sentence 1 is requested as soon as the player pulls the first chunk; chunks are
  yielded the moment they arrive.
- **Pipelining (prefetch):** a background thread downloads sentence N+1 into a bounded queue
  (`prefetch_chunks`, default 64 ≈ 6 s) while sentence N plays.
- **Interrupt:** `SpeechStream.cancel()` / `.close()` (and `SarvamTTS.cancel()` for all streams) end the
  iteration immediately, close the HTTP response and stop the prefetch thread. `AudioPlayer.stop()` calls
  `cancel()` on the stream it is playing, so an Interrupt stops both playback and download.
- **Errors:** if a sentence fails on both endpoints, iterating raises `SarvamError`; `health()` turns red.

## Events (wired by the orchestrator)

| In | Handling |
|---|---|
| `SpeakRequest(text, language)` | `player.play(tts.stream(text, language))` |
| `Interrupt` | `player.stop()` (cancels the stream) or `tts.cancel()` |

Out: audio for the player; `SpeakStarted` / `SpeakFinished` come from `AudioPlayer.on_start/on_finish`;
`Metrics.tts_first_ms` from `play(on_first_audio=...)`; `HealthChanged` from `health()`.

## Settings read

`tts_model`, `tts_sample_rate`, `tts_pace`, `tts_speaker` (default voice), `tts_voices` via
`settings.voice_for(language)` (e.g. `TTS_VOICES=hi-IN:priya,ta-IN:kavya`).

## Fakes

```python
from tts.fake import MockTTS, SilentTTS
tts = SilentTTS(24000, ms_per_char=60, latency_ms=300, realtime=True)  # timing-true silence
tts = MockTTS(settings)                                                # prints the reply
```

Tests (no network, `httpx.MockTransport`): `pytest tests/unit/tts`.

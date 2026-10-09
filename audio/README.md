# WS1 Audio I/O (`audio/`)

Microphone capture, speaker playback, resampling, the UI level meter, and fakes.
Imports only `core/` (plus numpy/scipy/sounddevice).

| File | What it does |
|---|---|
| `mic.py` | `Microphone`: continuous capture in 100 ms chunks of 16 kHz mono int16 into a queue (`read(timeout)`, `drain()`). Device selection (`MIC_DEVICE`, `list_input_devices()`), per-chunk level (`on_level`), noise gate, mute while speaking (`MuteFlag`), automatic recovery after unplug/replug. |
| `speaker.py` | `AudioPlayer`: streaming playback that starts with the first TTS chunk, writes 50 ms slices so `stop()` takes effect in < 100 ms, mutes the mic for the whole reply and unmutes 300 ms after. |
| `resample.py` | `resample_int16(pcm, from_rate, to_rate)` (scipy polyphase) and `to_mono_int16`. Used when the mic only opens at 44.1/48 kHz. |
| `fake.py` | `FakeMicrophone` (replays WAV files as if spoken) and `FakeSpeaker` (records instead of playing). |

## Behaviour

- **Device errors** never crash: `Microphone.start()` raises `RuntimeError("Microphone not available: ...")`
  with a friendly hint (no mic, unsupported rate, mic in use, PortAudio DLL missing).
- **Sample rate:** opens at 16 kHz; if the device refuses, opens at its default rate and resamples each chunk.
- **Noise gate:** chunks below `noise_gate` are replaced by zeros but still queued (STT keep-alive and the
  local end-of-speech timer keep working). The level meter always gets the real level.
- **Recovery:** a watchdog notices a dead or stalled stream (no callback for 2 s), calls `on_error(msg)`,
  retries every 2 s (rescanning devices after the first failure, max 30 tries), then `on_recovered()`;
  after the last try `on_error("... Gave up ...")`. `health()` is red while the device is lost.
- **Self-hearing:** `AudioPlayer.play()` sets the shared `MuteFlag` at the start of every reply; chunks are
  dropped while muted; unmute happens `unmute_delay_s` (0.3 s) after playback ends.
- **Interrupt:** `AudioPlayer.stop()` is thread-safe, returns playback within one 50 ms slice, calls `cancel()`
  on the source if it has one (a `tts.SpeechStream`), and `play()` closes the source so TTS prefetch stops.
- **Dropped chunks** (queue of 100 = 10 s full) are counted in `Microphone.dropped` and shown in `health()`.

## Events (wired by the orchestrator)

| Out | From |
|---|---|
| `AudioChunk` | `Microphone.read()` (pulled by the STT thread) |
| `MicLevel` | `on_level(level)` callback |
| `SpeakStarted` | `AudioPlayer(on_start=...)`: first audio of a reply written |
| `SpeakFinished(interrupted)` | `AudioPlayer(on_finish=...)`: only after a reply that started |
| `ErrorRaised` / `HealthChanged` | `Microphone(on_error=..., on_recovered=...)`, `health()` |

| In | Handling |
|---|---|
| `Interrupt` | call `player.stop()` |
| `SpeakStarted` / `SpeakFinished` | mute/unmute is done by `AudioPlayer` through the shared `MuteFlag` |

## Settings read

`sample_rate`, `chunk_ms`, `mic_device`, `noise_gate` (via `Microphone.from_settings(settings, mute)`),
`tts_sample_rate` (player rate), `barge_in` (orchestrator passes no `MuteFlag` to the player when on).

## Fakes

```python
from audio.fake import FakeMicrophone, FakeSpeaker
mic = FakeMicrophone(realtime=True)          # replays tests/audio_samples/*.wav in 100 ms chunks
player = FakeSpeaker(24000, mute, realtime=True)   # records; player.audio / player.seconds_played
```

`tests/audio_samples/` holds small **synthetic** 16 kHz mono WAVs (tone + silence, speech-like bursts,
room noise) made by `python tests/audio_samples/make_samples.py`. No real recorded speech is available in
the repository; drop your own 16-bit WAVs into that folder (any rate/channels, they are converted) to test
with real voices. Tests: `pytest tests/unit/audio`.

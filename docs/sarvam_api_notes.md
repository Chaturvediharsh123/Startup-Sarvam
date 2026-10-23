# Sarvam API notes (verified against the official docs)

Checked on 2026-10-06. Every fact below cites the page it came from. Raw Markdown of any docs page is available by appending `.md` to its URL.
All Sarvam calls in this project must match this file. If the docs change, update this file first.

## Base URL and authentication

| Fact | Value | Source |
|---|---|---|
| Base URL | `https://api.sarvam.ai` | https://docs.sarvam.ai/api-reference/speech-to-text/transcribe |
| Auth header (all REST APIs) | `api-subscription-key: <key>` | https://docs.sarvam.ai/api/api-guides-tutorials/chat-completion/overview ("like every Sarvam API, this endpoint uses the `api-subscription-key` header") |
| Chat also accepts | `Authorization: Bearer <key>` (OpenAI-compatible tooling). We use `api-subscription-key` everywhere for consistency. | same page |
| WebSocket auth | `Api-Subscription-Key` header on the handshake (browsers can use subprotocol `api-subscription-key.<key>`) | https://docs.sarvam.ai/api-reference/speech-to-text/transcribe/realtime/ws |
| Error body | JSON `{"error": {"message", "code", "request_id"}}`. Codes include `invalid_api_key_error`, `rate_limit_exceeded_error`, `insufficient_quota_error`, `internal_server_error`. HTTP 400/403/422/429/500 (and 503 when overloaded). | https://docs.sarvam.ai/api-reference/chat/chat-completions-v1, https://docs.sarvam.ai/api/api-guides-tutorials/speech-to-text/rest-api |
| Retry guidance | Retry 429 and 5xx with backoff. Do not retry 400/403/422. | https://docs.sarvam.ai/api/api-guides-tutorials/speech-to-text/rest-api (error example) |

## Client library decision

The official SDK is `sarvamai` (latest on PyPI: 0.1.35). It covers every API we need. https://docs.sarvam.ai/api/getting-started/sdks

**Decision: do not use the SDK. Use `httpx` (REST and HTTP streaming) plus `websockets` (realtime STT).** Reasons:
- The docs publish the raw wire protocol for every endpoint we use, so a direct client is simple.
- We control timeouts, retries and reconnects in one place (`sarvam/client.py`).
- Tests can mock `httpx` and the WebSocket with no SDK internals.

## 1. Speech-to-Text, realtime WebSocket (live mode)

Sources: https://docs.sarvam.ai/api/api-guides-tutorials/speech-to-text/realtime-streaming and https://docs.sarvam.ai/api-reference/speech-to-text/transcribe/realtime/ws

- **URL:** `wss://api.sarvam.ai/speech-to-text-realtime/ws` (`GET /speech-to-text-realtime/ws`). This is the new realtime endpoint. The legacy endpoint `/speech-to-text/ws` has no partial transcripts; we do not use it.
- **Models:** `saaras:v3-realtime` (default) or `saaras:v4`. Both use the same parameters and messages. **`saaras:v3-realtime` exists, as named in the brief.**
- **Connection settings go in the query string:**
  - `language_code` (required): BCP-47 code or `auto` (adaptive detection). We use `auto`.
  - `model`: `saaras:v3-realtime`
  - `stream_type`: `fast` | `balanced` (default) | `simulated` (no partials). Docs recommend `fast` for voice agents.
  - `mode`: `transcribe` (default) | `translate` | `verbatim` | `translit` | `codemix`. Applies to the final transcript only.
  - `endpointing`: `vad` (default, server detects end of turn) | `manual`
  - `encoding`: `linear16` (default) | `linear32` | `mulaw` | `alaw`. Mono only.
  - `sample_rate`: `8000` or `16000` only (default `16000`). Any other value closes the socket with code `4000`.
  - `threshold` (0.3), `silence_duration_ms` (500), `min_speech_duration_ms` (250): VAD tuning, `vad` mode only.
  - `prompt`, `return_timestamps`, `keyterms` (JSON array, `saaras:v4` only)
- **Audio format:** raw 16-bit little-endian PCM (`linear16`), mono, 16 kHz. 100 ms = 3200 bytes. The docs example sends ~100 ms chunks.
- **Client to server messages (JSON text frames):**
  - `{"event": "audio_input", "audio": "<base64 PCM>"}`
  - `{"event": "ping"}`: keepalive
  - `{"event": "end"}`: graceful close
  - `{"event": "config.update", ...}`: change settings mid-call
  - `speech_start` / `speech_end` / `flush`: `manual` mode only
- **Server to client messages:**
  - `session.begin` (`request_id`, `config`)
  - `vad.speech_start` / `vad.speech_end` (`utterance_idx`, `confidence`)
  - `transcript.partial` (`utterance_idx`, `text`, `language` when `language_code=auto`)
  - `transcript.final` (`utterance_idx`, `text`, `language` and `language_confidence` when `auto`)
  - `config.updated`, `pong`
  - `session.end` (`audio_duration_s`, the billed audio)
  - `error` (`code`, `is_fatal`, `message`)
- **End of speech:** the server VAD finds it (`vad.speech_end`, then `transcript.final`). **No local silence detection needed.**
- **Close codes:** `1003` rate limit, quota or bad key (do not reconnect in a loop) · `1008` inactivity timeout ("send periodic pings") · `1011` server error (retry with backoff) · `4000` invalid parameter or account not enabled (do not retry).
- **Language codes accepted (24):** `auto`, `en-IN`, `hi-IN`, `bn-IN`, `kn-IN`, `ml-IN`, `mr-IN`, `or-IN`, `pa-IN`, `ta-IN`, `te-IN`, `gu-IN`, `ur-IN`, `ne-IN`, `kok-IN`, `ks-IN`, `sd-IN`, `sa-IN`, `sat-IN`, `mni-IN`, `brx-IN`, `mai-IN`, `doi-IN`, `as-IN`.
- **Watch out:** Odia is `or-IN` here but `od-IN` on the REST STT and TTS APIs.
- Best practice: drive barge-in from `vad.speech_start` or early partials, not from `transcript.final`.

## 2. Speech-to-Text, REST (push-to-talk and fallback)

Sources: https://docs.sarvam.ai/api-reference/speech-to-text/transcribe and https://docs.sarvam.ai/api/api-guides-tutorials/speech-to-text/rest-api

- **URL:** `POST https://api.sarvam.ai/speech-to-text`, `multipart/form-data`
- **Fields:**
  - `file` (required): WAV/MP3/AAC/OGG/FLAC/…; 16 kHz recommended; mono. We send WAV, 16-bit PCM.
  - `model`: `saaras:v4` (default, recommended) or `saaras:v3`. **`saaras:v4` exists, as named in the brief.**
  - `language_code`: `unknown` means auto-detect. Otherwise a BCP-47 code (`hi-IN`, `od-IN`, …).
  - `mode`: the API reference says it applies to `saaras:v3` only, while the guide shows it with v4. We leave it out, which gives the default `transcribe`.
  - `keyterms`: JSON array string (v4 only), `with_timestamps`, `input_audio_codec` (only for raw PCM)
- **Limit:** 30 seconds of audio per request (HTTP 422 if longer).
- **Response:** `{"request_id", "transcript", "language_code" (null if none detected), "language_probability" (when unknown/omitted)}`

## 3. Chat Completions (the brain)

Sources: https://docs.sarvam.ai/api-reference/chat/chat-completions-v1, https://docs.sarvam.ai/api/api-guides-tutorials/chat-completion/overview and https://docs.sarvam.ai/api/getting-started/models/sarvam-105b

- **URL:** `POST https://api.sarvam.ai/v1/chat/completions` (JSON). V1 serves only `sarvam-105b` and `sarvam-105b-conversations`.
- **Models:** `sarvam-105b` (128K context; "complex reasoning, agentic tool use") and `sarvam-105b-conversations` (32K context; "real-time dialogue, voice agents"). **`sarvam-105b` exists, as named in the brief.**
- **Request fields:** `model`, `messages` (roles: `system`, `developer`, `user`, `assistant` with optional `tool_calls`, `tool` with `tool_call_id`), `temperature` (default 0.2), `max_tokens` (default 2048), `reasoning_effort` (`low`/`medium`/`high`/`max`, or `null` to turn thinking off), `stream`, `tools`, `tool_choice` (`auto`/`none`/`required`/named), `response_format`, `stop`, `seed`.
- **Tool format: OpenAI style, confirmed.** `{"type": "function", "function": {"name", "description", "parameters": <JSON Schema object>}}`. Only `function` tools exist. Legacy `functions`/`function_call` are rejected (stated on V2; we use `tools` on V1 anyway).
- **Response:** `choices[0].message` = `{role: "assistant", content (nullable when tool_calls exist), reasoning_content, tool_calls: [{id, type: "function", function: {name, arguments: <JSON string>}}]}`. `finish_reason` is one of `stop`, `length`, `tool_calls`, `content_filter`.
- **Tool loop (documented):** send messages plus tools. If `finish_reason == "tool_calls"`, run each tool. Append the assistant message (with `tool_calls`) and one `{"role": "tool", "tool_call_id", "content"}` per call. Call again for the final answer.
- **Parallel tool calls:** `parallel_tool_calls: false` is **not** enforced on `sarvam-105b`. The model may return several tool calls at once, so the brain must handle a list.
- **Thinking mode is on by default.** Reasoning tokens count against `max_tokens`. With a small `max_tokens`, `content` can come back empty with `finish_reason: "length"`. The docs disagree on the default (API reference says `medium`; guide says `low`). We set `reasoning_effort` explicitly (default `low`, configurable, `none` turns it off) and keep `max_tokens` at 1024 or more.
- Temperature default is 0.5 with reasoning on and 0.2 with reasoning off (model page limits table).

## 4. Text-to-Speech, REST (fallback)

Source: https://docs.sarvam.ai/api-reference/text-to-speech/convert

- **URL:** `POST https://api.sarvam.ai/text-to-speech` (JSON)
- **Fields:** `text` (bulbul:v3 max 2500 characters), `language_code` (required), `speaker`, `model` (`bulbul:v3`), `pace` (v3: 0.5–2.0), `temperature` (v3 only, 0.01–1.0, default 0.6), `speech_sample_rate` (default 24000 for v3), `output_audio_codec` (default `wav`).
- **Response:** `{"request_id", "audios": ["<base64 WAV>", ...]}`

## 5. Text-to-Speech, streaming

Sources: https://docs.sarvam.ai/api/api-guides-tutorials/text-to-speech/which-api-to-use, https://docs.sarvam.ai/api-reference/text-to-speech/convert-stream, https://docs.sarvam.ai/api/api-guides-tutorials/text-to-speech/streaming-api/http-stream and https://docs.sarvam.ai/api-reference/text-to-speech/stream

There are two streaming transports with the same audio quality:
- **HTTP stream:** `POST https://api.sarvam.ai/text-to-speech/stream`. One request returns a **raw binary audio stream** (not JSON, not base64). Max 3500 characters. Audio "starts arriving as soon as the first audio chunk is ready".
- **WebSocket:** `GET wss://api.sarvam.ai/text-to-speech/ws`. Config message first, then text messages. Returns base64 chunks. Best for streaming LLM tokens on a warm connection.

**Decision: use the HTTP stream.** Our LLM reply is complete before we speak it (1–2 sentences), so token streaming brings little. HTTP stream needs no connection management. We send one request per sentence so the first sentence plays quickly.

- **HTTP stream fields:** `text` (required), `language_code`, `speaker`, `model` (**default is `bulbul:v2`, so we must send `bulbul:v3`**), `pace`, `temperature`, `speech_sample_rate` (8000/16000/22050/24000/32000/44100/48000; above 24000 is REST only, not streaming), `output_audio_codec` (`mp3` default, `linear16`, `wav`, `opus`, `flac`, `aac`, `mulaw`, `alaw`), `output_audio_bitrate`, `enable_preprocessing`, `dict_id`.
- **We request:** `model=bulbul:v3`, `output_audio_codec=linear16`, `speech_sample_rate=24000` (the bulbul:v3 default per https://docs.sarvam.ai/api-reference/text-to-speech/stream). That gives uncompressed 16-bit PCM we can play directly, with no MP3 decoder.
- **Not stated in the docs:** whether the `linear16` stream carries a WAV header. `tts.py` checks the first bytes for `RIFF` and strips the header if present. I will confirm this with a live call in Phase 6.
- **bulbul:v3 notes:** no `pitch`/`loudness`; pace 0.5–2.0; preprocessing always on; default speaker `shubh`.
- **bulbul:v3 speakers (lowercase, case-sensitive):** shubh (default), aditya, ritu, priya, neha, rahul, pooja, rohan, simran, kavya, amit, dev, ishita, shreya, ratan, varun, manan, sumit, roopa, kabir, aayan, ashutosh, advait, anand, tanya, tarun, sunny, mani, gokul, vijay, shruti, suhani, mohit, kavitha, rehan, soham, rupali. Docs rate `priya`, `ishita` and `mani` as Tier 1 for pronunciation, and warn that `varun` is not a neutral voice. Source: https://docs.sarvam.ai/api/api-guides-tutorials/text-to-speech/how-to/change-the-speaker-voice
- **TTS language codes (11 only):** `bn-IN`, `en-IN`, `gu-IN`, `hi-IN`, `kn-IN`, `ml-IN`, `mr-IN`, `od-IN`, `pa-IN`, `ta-IN`, `te-IN`. STT detects 22+ languages, so `tts.py` maps any other code to a supported one (`ur-IN`, `mai-IN`, `ne-IN`, … become `hi-IN`; anything else becomes `en-IN`).
- Numbers over 4 digits should use commas ("10,000") for correct pronunciation.

## Differences from BUILD_PROMPT.md

1. **LLM model.** `sarvam-105b` exists. The docs also offer `sarvam-105b-conversations`, which they recommend for voice agents. The README already names it. I keep `sarvam-105b` as the default (the docs explicitly mention its "agentic tool use"). The model is a config value (`SARVAM_LLM_MODEL`), so switching is one line in `.env`.
2. **TTS transport.** The brief says "streaming TTS". I use the documented HTTP stream (`/text-to-speech/stream`), not the WebSocket. Reason above.
3. **End of speech.** The realtime STT has server VAD with `vad.speech_end` and `transcript.final`. No local 700 ms silence detector is needed. `silence_duration_ms` (default 500) is configurable.
4. **Sample rates.** Mic input is 16000 Hz (STT allows only 8000/16000). TTS output is 24000 Hz.
5. **Odia code.** `or-IN` on realtime STT, `od-IN` on REST STT and TTS. The code normalises between them.
6. **REST STT auto-detect** uses `language_code=unknown`. Realtime STT uses `auto`.
7. **Keepalive.** Realtime STT closes idle sockets (code 1008). The mic is muted while the assistant speaks, so `stt_realtime.py` sends `{"event":"ping"}` during silence.

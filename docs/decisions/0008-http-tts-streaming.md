# 0008. HTTP TTS streaming instead of WebSocket

- Status: Accepted
- Date: 2026-10-08

## Context

Sarvam offers two streaming TTS transports with the same audio quality
(`docs/sarvam_api_notes.md`, section 5): an HTTP stream (`POST /text-to-speech/stream`,
raw binary audio, up to 3500 characters per request) and a WebSocket
(`/text-to-speech/ws`, a config message then text messages, base64 chunks back), which
suits streaming LLM tokens over a warm connection.

## Decision

Use the **HTTP stream**, one request per sentence, with `model=bulbul:v3`,
`output_audio_codec=linear16` and `speech_sample_rate=24000`.

- The LLM reply is complete before we speak it (1-2 sentences), so token streaming gains
  little.
- HTTP needs no connection management, reconnects or keep-alives, and is easy to mock with
  `httpx`.
- One request per sentence still gets the first sentence playing quickly (ADR 0007).
- `linear16` gives raw 16-bit PCM we can play directly; a `RIFF` header is stripped if
  present.

## Consequences

- One TLS request per sentence; acceptable for short replies.
- Revisit the WebSocket if replies get long or LLM token streaming is adopted.
- TTS accepts only 11 language codes; `language.detect.to_tts_language` maps the rest.

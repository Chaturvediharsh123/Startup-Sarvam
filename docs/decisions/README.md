# Architecture decision records

Short, numbered records of decisions that shape the code. Each has Context, Decision and
Consequences. To change a decision, add a new ADR that supersedes the old one instead of
rewriting it.

| No. | Decision | Status |
| --- | --- | --- |
| [0001](0001-module-layout-and-event-bus.md) | Module layout; threads + thread-safe event bus now, asyncio later | Accepted |
| [0002](0002-hinglish-script.md) | Hinglish in Roman script; Devanagari Hindi stays Devanagari | Accepted |
| [0003](0003-result-phrasing-templates.md) | Result phrasing via per-action templates, no second LLM call | Accepted |
| [0004](0004-one-action-per-turn.md) | At most one action per turn | Accepted |
| [0005](0005-confirmation-timeout-and-history.md) | Confirmation timeout 10 s; history 4 turns | Accepted |
| [0006](0006-settings-env-and-allowlist.md) | settings.yaml + .env (key only in .env); allowlist.yaml owned by Safety | Accepted |
| [0007](0007-sentence-prefetch-and-early-reply.md) | Sentence prefetch and early in-progress reply for latency | Accepted |
| [0008](0008-http-tts-streaming.md) | HTTP TTS streaming instead of WebSocket | Superseded in part by 0009 |
| [0009](0009-deepgram-stt-cartesia-tts.md) | Deepgram STT and Cartesia TTS; LLM chosen by golden-set eval | Accepted |

New ADR: copy the newest file, take the next number, keep it under a page, and add it to
this table.

# 0007. Sentence prefetch and early "in-progress" reply for latency

- Status: Accepted
- Date: 2026-10-08

## Context

The user should hear something within about 1.5 s of finishing speaking. A turn is STT
final -> LLM -> safety -> action -> TTS, and LLM time plus TTS first-byte time dominate.
Waiting for the action and then synthesising the whole reply is too slow.

## Decision

- **Early in-progress reply:** as soon as the brain decides, its short reply ("Chrome khol
  raha hoon") goes to TTS while the action runs. If the LLM is slow, a canned filler
  phrase plays after `filler_after_ms`.
- **Sentence prefetch:** replies are split into sentences; sentence N+1 is requested from
  TTS while sentence N plays, so there is no gap between sentences.
- The result template (ADR 0003) is queued after the in-progress reply.
- Barge-in or cancel stops playback and drops prefetched audio.

## Consequences

- Perceived latency is roughly LLM time + first TTS chunk, not the whole pipeline.
- Up to two TTS requests are in flight per turn; rate limits must allow it.
- The early reply must never claim success; the template confirms the real outcome.

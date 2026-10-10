# 0009. Deepgram for speech-to-text, Cartesia for text-to-speech

- Status: Accepted
- Date: 2026-10-11
- Supersedes: the Sarvam parts of 0008 (Bulbul HTTP streaming)

## Context

The project brief chose Deepgram for STT and Cartesia for TTS. The code on `main` uses
Sarvam for all three AI parts (Saaras STT, `sarvam-105b` LLM, Bulbul TTS). We are
switching STT and TTS to match the brief. The LLM is a separate decision (see below).

Checked against the official docs on 2026-10-11:

- Deepgram `nova-3` supports Hindi (`hi`) and `multi`, a code-switching mode whose ten
  languages include English and Hindi. `flux-general-multi` (streaming) covers the same
  set. `nova-2`'s `multi` is Spanish + English only, so it is not an option.
- Cartesia `sonic-3.6` supports Hindi (`hi`) and 10 more Indian languages. Its docs say
  it has expanded support for Hindi written in Latin script (Hinglish).

Not verified yet: how well either one handles Hindi-English mixing within one sentence.
We will measure that on recorded speech before relying on it.

## Decision

- **STT:** Deepgram `nova-3` with `language=multi`. Start with push-to-talk on recorded
  clips (pre-recorded API), then move to streaming once the text pipeline passes.
  Recorded clips are repeatable, so STT errors can be told apart from brain errors.
- **TTS:** Cartesia `sonic-3.6`, pinned to a dated snapshot in production.
- **Language codes:** inside the app we keep BCP-47 codes (`hi-IN`, `en-IN`). Mapping to
  provider codes (`hi`, `en`) happens only at the edges, in `language/`.
- **Keys:** `DEEPGRAM_API_KEY` and `CARTESIA_API_KEY` in `.env` only, never in YAML or code.
- **LLM:** not decided here. `sarvam-105b` remains a candidate. The choice will come from
  the golden-set evaluation (`tools/golden_eval.py`).
- **Order:** this is Phase 5. The new clients are built after the text-only language
  layer passes its gate (>= 90% on en + hi + Hinglish). Until then the Sarvam clients
  and the fakes stay in place, and nothing is deleted.

## Consequences

- `stt/` and `tts/` get new Deepgram and Cartesia clients behind the existing
  interfaces in `core/interfaces.py`. Fakes and mock mode are unchanged.
- `core/config.py` must stop requiring `SARVAM_API_KEY` when the LLM is not Sarvam
  (tech lead review, `core/` is owned by the tech lead).
- `language.detect.to_tts_language` and `to_realtime_stt_code` are Sarvam-specific and
  will be replaced by provider code mappers.
- README, `.env.example` and `config/settings.yaml` need updating once the clients land.
- Two paid speech vendors plus an LLM vendor: track cost per turn during evaluation.

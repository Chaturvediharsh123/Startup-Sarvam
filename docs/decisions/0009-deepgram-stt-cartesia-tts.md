# 0009. Deepgram for speech-to-text, Cartesia for text-to-speech

- Status: Accepted
- Date: 2026-10-11 (updated 2026-10-11 after live mic tests)
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

How each one handles Hindi-English mixing within one sentence was measured with live mic
tests (see "Live test results" below).

## Decision

- **STT:** Deepgram `nova-3`.
  - Push-to-talk (`stt/deepgram_batch.py`): `language=multi`.
  - Live streaming (`stt/deepgram_realtime.py`): `language=hi`, `endpointing=800` (500 cut
    sentences at natural mid-sentence pauses in the full-loop test, #15). In
    this mode Deepgram tags no word languages, so the language is detected from the
    transcript with `language.detect.detect_language`.
- **TTS:** Cartesia `sonic-3.6`, pinned to a dated snapshot in production.
- **Language codes:** inside the app we keep BCP-47 codes (`hi-IN`, `en-IN`). Mapping to
  provider codes (`hi`, `en`) happens only at the edges, in `language/`.
- **Keys:** `DEEPGRAM_API_KEY` and `CARTESIA_API_KEY` in `.env` only, never in YAML or code.
- **LLM:** not decided here. `sarvam-105b` remains a candidate. The choice will come from
  the golden-set evaluation (`tools/golden_eval.py`).
- **Order:** the speech clients are built in parallel with the brain work, because they
  sit behind the interfaces in `core/interfaces.py`. Each client gets its own PR. The
  Sarvam clients and the fakes stay in place until the wiring PR switches `main.py`
  over, and nothing is deleted before then.

## Live test results (2026-10-11, one speaker, laptop mic)

| | Push-to-talk, `multi` | Streaming, `multi` | Streaming, `hi` |
|---|---|---|---|
| "Notepad kholo" | `Notepad खोलो.` | `Notepad colo` or `Notepad.` | `Notepad खोलो.` |
| "Volume chalees kar do" | `Volume चालीस कर दो.` | `Volume.` 2 of 3 times (800 ms endpointing) | `Volume चालीस कर दो.` |
| "Set volume to forty percent" | correct | correct | correct, Roman letters |
| Delay | 1.5-2.2 s after a 5 s clip | ~0.8-1.2 s (500 ms), ~1.2-2.5 s (800 ms) | ~0.5-1.2 s |

- `multi` works for whole recordings but in streaming it often heard short Hindi words as
  English or dropped them. A longer `endpointing` (800 ms) was slower and did not fix it.
- Real transcripts are mixed-script: English words in Roman letters, Hindi words in
  Devanagari. App names occasionally arrive in Devanagari (`नोटपैद`).
- Names are misheard ("Arijit" as अर्जित / अद्वित). Keyterm prompting is still to be tried.
- Cartesia `sonic-3.6`: first audio in ~150-230 ms (609 ms on the first ever request);
  Roman-script Hinglish sounds close to natural Hindi, so ADR 0002 stands.

## Consequences

- `stt/` and `tts/` have new Deepgram and Cartesia clients behind the existing
  interfaces in `core/interfaces.py` (#10, #11, #13). Fakes and mock mode are unchanged.
- The golden set should include mixed-script transcripts, since that is what STT sends.
- `core/config.py` must stop requiring `SARVAM_API_KEY` when the LLM is not Sarvam
  (tech lead review, `core/` is owned by the tech lead).
- `language.detect.to_tts_language` and `to_realtime_stt_code` are Sarvam-specific and
  will be replaced by provider code mappers.
- README, `.env.example` and `config/settings.yaml` need updating once the clients land.
- Two paid speech vendors plus an LLM vendor: track cost per turn during evaluation.

# Language layer checklist

Milestone 1: text in (English, Hindi, Hinglish) -> validated tool call or a clarifying
question, measured on the golden set with a real model. No speech, no PC actions.

Only verified items are ticked. "Verified" means a test or eval run, noted with its date.

## Done
- [x] Language detection en / hi / Hinglish (`language/detect.py`). Unit tests pass (2026-10-10).
- [x] Structured output = one LLM tool call, at most one per turn (`brain/brain.py`, ADR 0004).
- [x] Argument validation: types, enums, ranges, required, no extras (`safety/guard.py`).
- [x] Bad or failed LLM answer becomes a spoken apology, never a crash (`brain/brain.py`).
- [x] Golden set + evaluator (`tests/golden/utterances.yaml`, `tools/golden_eval.py`).
- [x] Clarification cases: 15 items (5 en, 5 hi, 5 Hinglish), scored with
      `expect_clarify` (2026-10-10).
- [x] Follow-up "open it again" resolved from history (fu-009, fu-010) (2026-10-10).
- [x] Full suite: 830 passed, ruff clean, FakeLLM only (2026-10-10).

## Next
- [ ] Decide the stack: Sarvam (in the code now) or Deepgram + Cartesia (in the brief).
- [ ] Get an LLM API key into `.env` (never commit it).
- [ ] Baseline the real model: `python tools/golden_eval.py --lang en --lang hi --lang hinglish`.
- [ ] Shortlist 2-3 candidate models; add an OpenAI-compatible adapter behind the
      brain's `chat(messages, tools)` interface if a non-Sarvam model is shortlisted.
- [ ] Compare accuracy, clarify rate, wrong-tool rate, p95 latency, cost per 1k turns.
- [ ] Fix failures in the prompt or few-shots; add every new failure to the golden set.
- [ ] Gate: >= 90% on en + hi + Hinglish before starting speech integration.

## Open questions
- "Type this into Notepad: ..." needs two actions (open, then type). The one-action-per-turn
  rule (ADR 0004) blocks it. Options: a `target` arg on `type_text`, or ask the user to
  open the app first.
- The clarify check only looks for "?". A reply such as "App ka naam boliye." is a valid
  clarification but is scored as a fail. Review clarify failures by hand until this matters.

# 0003. Result phrasing via templates, no second LLM call

- Status: Accepted
- Date: 2026-10-08

## Context

After an action runs, the assistant must say what happened ("Volume 40 kar diya"). A
second LLM call to phrase the result adds roughly 1-2 s per turn, costs tokens and can
claim things that did not happen.

## Decision

- Each action returns a short English `fact` (for example `volume 40`) in `ActionResult`.
- The spoken result comes from **per-action templates** in the user's language and script
  (`language/phrases.py`), filled in with that fact. Failures, denials and timeouts also
  use templates.
- The LLM is called once per turn: it picks the tool and writes the in-progress or
  conversational reply. There is no second "summarise" call.

## Consequences

- Lower, more predictable latency; replies never contradict the real outcome.
- Every new action must add its templates for all supported languages (part of the
  Definition of done).
- Informational results (time, battery) also come from templates, so they sound less
  natural than free LLM text. Acceptable for V1.

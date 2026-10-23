# 0005. Confirmation timeout 10 s; conversation history 4 turns

- Status: Accepted
- Date: 2026-10-08

## Context

High-risk actions (shutdown, closing apps with unsaved work) need a spoken "yes" before
they run, and elderly users may answer slowly. The LLM prompt also carries recent turns so
that follow-ups like "ab isko band karo" work, but every extra turn adds tokens and latency.

## Decision

- The safety guard waits up to **10 seconds** (`confirm_timeout_s`) for a clear yes. No
  answer, an unclear answer or a timeout means **no**.
- The brain sends the last **4 turns** (`history_turns`) plus the last action and known
  facts from memory.
- Both are settings in `config/settings.yaml` (overridable in `.env`), validated in
  `core/config.py` (timeout 1-60 s, history 0-100 turns).

## Consequences

- Safe default: silence never triggers a risky action.
- Four turns cover the follow-up patterns in the golden set (`fu-*` items) while keeping
  prompts short; older references ("the app from ten minutes ago") will not resolve.

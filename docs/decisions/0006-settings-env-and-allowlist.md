# 0006. settings.yaml + .env; allowlist.yaml owned by Safety

- Status: Accepted
- Date: 2026-10-08

## Context

Settings were spread over environment variables. Non-developers need a readable file to
tune voices, timeouts and the UI; the API key must never be committed or logged; and the
list of what the assistant may do is a safety decision, not a feature toggle.

## Decision

- `config/settings.yaml` holds all non-secret settings, grouped by module, committed with
  safe defaults.
- `.env` (git-ignored, template in `.env.example`) holds the **Sarvam API key only**, plus
  personal overrides. `core/config.py` refuses a key found in `settings.yaml`.
- Precedence: `settings.yaml` < `.env` < environment variables.
- `config/allowlist.yaml` lists every allowed action, argument rule, app, alias and
  shortcut. **Safety owns it** (CODEOWNERS): actions read it, the guard enforces it, and
  anything not listed is denied.

## Consequences

- Keys stay out of git, logs and CI.
- Adding an app or action needs a Safety-reviewed change to `allowlist.yaml`.
- Every new setting must be documented in `settings.yaml` (Definition of done).

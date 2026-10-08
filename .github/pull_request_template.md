## What and why

<!-- One or two sentences: what this PR changes and why. Link the issue / workstream (WS1-WS6). -->

## How it was tested

<!-- Commands you ran, utterances you tried, languages covered. -->

## Definition of done

- [ ] Follows the module template (start / stop / health) and uses only the shared events and interfaces in `core/` (no new cross-module imports)
- [ ] Unit tests with fakes added or updated, and they pass in CI (`ruff check .` and `pytest -q` are green)
- [ ] Works in Hindi, English and Hinglish; speech features (STT / TTS / brain phrasing) also tried in at least one South Indian language (Tamil, Telugu, Kannada or Malayalam)
- [ ] Logged with the turn id; no API keys, raw audio or full transcripts of personal data in logs
- [ ] Risky behaviour (closing apps, typing, shutdown, files, new actions) reviewed by the safety owner and run in the Windows Sandbox (`sandbox/`)
- [ ] README updated and any new setting documented in `config/settings.yaml` / `.env.example`
- [ ] Brain or action changes: golden set re-run (`python tools/golden_eval.py`) and accuracy did not drop

## Reviewers

- 1 approving review is required before merge.
- Changes under `core/` need the tech lead's review (enforced by `.github/CODEOWNERS`).

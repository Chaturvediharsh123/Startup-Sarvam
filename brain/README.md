# Brain (WS3)

**Mission:** understand what the user wants, pick **at most one** allowed action, and write a
short spoken reply in the user's language and script — in **one** LLM call.

The brain never runs PC actions and never imports `actions/` or `safety/`. The orchestrator
sends its tool call to the safety guard and the ActionRunner, then asks the brain to phrase
the result.

## Files

| File | What it holds |
|---|---|
| `brain.py` | `SarvamBrain` (implements `core.interfaces.Brain`) and `summarise_outcome()` |
| `prompts.py` | System prompt sections + few-shot examples (hi, Hinglish, en, ta, te, bn, kn) |
| `tools_schema.py` | Brain-only tools (`remember_fact`, `clear_conversation`) and `build_tools()` |
| `llm_client.py` | `SarvamLLM` (temperature 0.2, hard per-request timeout) and `ChatModel` Protocol |
| `memory.py` | SQLite memory: turns, actions, facts (schema v2) |
| `fake.py` | `FakeLLM` (keyword rules, alias `MockLLM`) and `FakeBrain` (scripted results) |

## In and out

```python
brain = SarvamBrain(memory, llm, settings, action_runner.tool_schemas())
result = brain.decide(text, stt_language, cancel)   # -> core.events.BrainResult
# result.reply      spoken now, phrased "in progress" ("Chrome khol raha hoon")
# result.tool_call  core.events.ToolCall | None  (only the FIRST PC tool the LLM asked for)
# result.language / result.romanised / result.llm_ms
outcome = action_runner.run(approved_call)           # -> core.events.ActionResult
sentence = brain.summarise(result, outcome)          # fixed template, user's language
brain.record_action(outcome)                         # last ok action = what "isko" means
brain.record_turn(text, spoken_reply, result.language)
```

* `decide` never raises. LLM failure or timeout -> `reply = phrase("sorry_repeat")`,
  `tool_call = None`, and `health().ok` turns False until the next good call.
* `remember_fact` / `clear_conversation` run inside `decide`; after a clear, the next
  `record_turn` is skipped so "sab bhool jao" itself is not remembered.
* `summarise` covers statuses `ok`, `error` (`not_installed`), `denied`, `blocked`
  (`rate_limited` when `extra["rate_limited"]`), `timeout`, `cancelled`, and fills in the
  numbers of `system_status` and `current_time`. No second LLM call.
* Lifecycle: `start()`, `stop()`, `health()`. Memory is owned and closed by `main`.

## Settings read

`history_turns` (turns of history sent, default 4), `default_language`, `language_hint`
(used when STT gives no language), `llm_timeout_s` (via `SarvamLLM.from_settings`),
`llm_model`, `llm_reasoning`, `shutdown_delay_s` (fallback number in the shutdown sentence).

## Run with fakes

```python
from brain.brain import SarvamBrain
from brain.fake import FakeLLM, FakeBrain
from brain.memory import Memory
from core.config import load_settings

settings = load_settings(require_key=False)
memory = Memory(":memory:")
brain = SarvamBrain(memory, FakeLLM(memory), settings, tool_schemas)  # offline, keyword rules
print(brain.decide("Chrome kholo", None))
```

`FakeLLM` reads app names, closable apps and shortcuts from the `tools` it is given (an
empty list means built-in defaults). `FakeBrain({"Chrome kholo": BrainResult(...)})` is a
scripted brain for orchestrator tests.

Tests: `python -m pytest -q tests/unit/brain`.

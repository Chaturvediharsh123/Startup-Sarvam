# Safety (WS5)

Decides **allow / ask first / deny** for every tool call, judges spoken yes/no answers,
watches for the emergency stop and keeps the audit log. Safety imports only `core` and
`language`; it never imports `actions` or `brain`.

## Files

| File | What it holds |
|---|---|
| `allowlist.py` | `Allowlist`: reads `config/allowlist.yaml` (risk per action, argument rules, apps + aliases, blocked apps, safe shortcuts, blocked key combos, limits) |
| `guard.py` | `SafetyGuard.check(call, language, romanised)` -> `SafetyDecision`; `RateLimiter`; `validate_args` |
| `confirm.py` | `is_yes`, `is_no` (multilingual), `confirmation_question(name, args, language, romanised)`, `ask_yes_no(listen, question, timeout_s)` |
| `kill_switch.py` | `KillSwitch(on_trigger)`: pyautogui failsafe + corner watcher thread; `enable_failsafe()` |
| `audit_log.py` | `AuditLog(path)`: one JSON line per request / decision / result; `recent(n)` for the UI |

## Rules

1. Emergency stop active (`guard.halt()`) -> DENY.
2. Tool not in the allow-list (or not registered) -> DENY "not on the allow-list".
3. Arguments: JSON-schema types + allow-list rules (app must resolve to an allowed app,
   shortcut must be on the safe list and not a blocked combo such as Alt+F4, Win+R or
   any Delete combo, text within limits) -> DENY "bad arguments: ...".
4. More than `max_actions_per_minute` -> DENY "rate limit: ...".
5. Risk: low -> ALLOW; medium -> ALLOW + logged; high -> CONFIRM with a spoken question in
   the user's language. Only a clear yes ("haan", "haan ji", "yes", "aam", "avunu", "ho")
   runs it; anything else, silence or the timeout is no.

`clean_args` carries canonical values (`"crome"` -> `"chrome"`, `"ctrl+s"` -> `"save"`).

## In / out

* In: `ToolCall` (from `BrainResult.tool_call`), the user's yes/no text.
* Out: `SafetyDecision` (verdict, reason, question, risk, clean_args). `AuditLog.handle`
  subscribes to `BrainResult`, `SafetyDecision` and `ActionResult` on the bus.
  `KillSwitch` calls its `on_trigger` (wire to `guard.halt`, `runner.halt` and the turn's
  cancel token).

## Settings read

`max_actions_per_minute`, `allowlist_path`, `confirm_timeout_s` (pass to `ask_yes_no`),
`log_dir` (`AuditLog.from_settings` writes `logs/audit.jsonl`, API key redacted).

## Wiring

```python
runner = ActionRunner(settings)
guard = SafetyGuard(settings, schemas_provider=runner.tool_schemas)
audit = AuditLog.from_settings(settings)          # bus.subscribe(..., audit.handle)
kill = KillSwitch(lambda: (guard.halt(), runner.halt()))
```

Tests: `python -m pytest -q tests/unit/safety tests/redteam` (the red-team file
`tests/redteam/redteam.yaml` holds 70+ hostile tool calls; all are denied, high-risk ones
ask first, and the runner is proven never to run for them).

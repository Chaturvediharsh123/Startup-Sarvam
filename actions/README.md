# Actions (WS4)

Does things on the PC, reliably, **only through named functions in one registry**.
No action can run a shell command or open an arbitrary path.

## Files

| File | What it holds |
|---|---|
| `registry.py` | `@action` decorator, `ACTIONS`, `ActionSpec` (name, description, JSON-schema args, risk, function), `tool_schemas()`, allow-list loading, the two PC wrappers `_startfile` / `_lock_workstation` |
| `apps.py` | `open_app`, `close_app`; `resolve_app()` (aliases, Hindi/other scripts, fuzzy match, blocked names); one-time Start Menu scan |
| `web.py` | `web_search`, `youtube_search` (fixed Google/YouTube URLs) |
| `system.py` | `set_volume`, `change_volume`, `take_screenshot`, `system_status`, `lock_pc`, `shutdown_pc` (60 s minimum, cancellable), `cancel_shutdown`, `current_time` |
| `typing_keys.py` | `type_text` (clipboard paste, any script), `press_shortcut` (safe list only), `scroll` |
| `notes.py` | `save_note` (sanitised name, always inside the notes folder, then opened) |
| `runner.py` | `ActionRunner`: runs one call in a worker thread with a timeout; never raises |
| `fake.py` | `FakeActionRunner`: same tool schemas, records calls, always succeeds |

`import actions` registers all 16 actions.

## In / out

* In: an approved `core.events.ToolCall`. Build it from the guard's decision:
  `ToolCall(call.name, decision.clean_args)`. Only run it when the verdict is ALLOW,
  or CONFIRM followed by a clear yes.
* Out: `core.events.ActionResult` with `status` `ok | error | timeout | cancelled`, a
  short English `fact` ("opened chrome", "volume 40", "photoshop not installed"),
  `informational`, `duration_ms` and `extra["error"]` for diagnostics. The orchestrator
  sets `turn_id` and publishes it.

## Settings read

`action_timeout_s` (default timeout), `notes_dir` (notes and screenshots),
`shutdown_delay_s` (never less than 60), `allowlist_path` (app aliases, shortcut keys,
text limits from `config/allowlist.yaml`, which Safety owns).

## Adding an action

One function with `@action("Plain description.", {...json schema...}, required=[...], risk="low")`
in the right file, plus one entry in `config/allowlist.yaml`. The LLM tool list updates itself.

## Running the fake

```python
from actions import FakeActionRunner
from core.events import ToolCall

runner = FakeActionRunner({"open_app": ("error", "photoshop not installed")})
runner.run(ToolCall("shutdown_pc"))   # ok, nothing happens
runner.names                          # ["shutdown_pc"]
```

Tests: `python -m pytest -q tests/unit/actions` (pyautogui, pycaw, psutil, `os.startfile`,
subprocess and the browser are all mocked; `tests/unit/actions/conftest.py` stops the real
Start Menu from being read).

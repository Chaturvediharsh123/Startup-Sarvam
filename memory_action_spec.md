# Memory + Action Block

## Final Engineering Specification

**Project:** Agentic Desktop + Browser Automation System
**Components owned:** Memory Block + Action Block
**Language:** Python
**Primary goal:** Build two modular, independently testable infrastructure components that integrate cleanly with the larger agent system.

---

# 1. Purpose

The larger application may contain:

* Planner
* LLM/agents
* Orchestrator
* Safety/approval layer
* Verification layer
* UI
* Voice
* APIs
* Authentication
* Other application-specific logic

Those components are **outside the ownership boundary** of this work.

This work owns only:

1. **Memory Block**
2. **Action Block**

The two blocks must expose clean APIs and must not become tightly coupled to the planner or business logic.

---

# 2. Core Architecture

```text
                         USER
                           |
                           v
                        PLANNER
                       /       \
                      /         \
                     v           v
                 MEMORY        ACTION
                    |             |
                    |             v
                    |         COMPUTER
                    |             |
                    v             v
                 CONTEXT <----- RESULT
                    |
                    v
              MEMORY UPDATE
```

### Memory answers:

> What information from previous/current interactions is relevant to this task?

### Action answers:

> How can I safely execute this specific computer interaction?

### Planner answers:

> What should happen next?

These responsibilities must remain separate.

---

# 3. Ownership Boundaries

## Memory owns

* Working memory
* Persistent memories
* Facts
* Preferences
* Episodes
* Observations
* Memory lifecycle
* Memory persistence
* Retrieval
* Hybrid search
* Provenance
* Confidence
* Verification timestamps
* Expiration
* Forget/delete operations
* Workflow storage and retrieval
* Workflow verification history

## Action owns

* Action schemas
* Action validation
* Action capability registry
* Action routing
* Browser interaction
* Windows application interaction
* Visual interaction fallback
* Screenshots
* Downloads
* Timeouts
* Limited retries
* Preconditions
* Postconditions/expected outcomes
* Execution evidence
* Structured ActionResult

## Memory does NOT own

* Planning
* LLM reasoning
* Browser automation
* Windows automation
* Business logic
* User interface
* Voice
* Agent orchestration

## Action does NOT own

* Long-term memory
* Planning
* LLM reasoning
* User preferences
* Task decomposition
* Business decisions
* Workflow learning
* Arbitrary Python/shell execution

---

# 4. Technology Stack

Use the following stack unless an existing repository constraint makes a change necessary.

## Common

* Python
* Pydantic
* asyncio where appropriate
* Type hints
* Structured logging

## Memory

* SQLite — persistent source of truth
* SQLite FTS5 — exact/keyword search
* LanceDB — semantic/vector search
* Local embedding model
* Python in-memory structures — working memory

## Action

### Browser

* Playwright

### Windows applications

* Windows UI Automation / pywinauto

### Visual fallback

* PyAutoGUI

### Screenshots

* mss
* Pillow

---

# 5. Avoid Premature Infrastructure

Do NOT introduce:

* Redis
* Kafka
* Mem0
* LangGraph
* another vector database
* another browser automation framework
* another desktop automation framework

unless there is a demonstrated requirement that cannot reasonably be solved using the current architecture.

The goal is:

```text
simple
+
modular
+
testable
+
replaceable
```

not unnecessary infrastructure complexity.

---

# 6. Suggested Repository Structure

```text
memory/
├── __init__.py
├── manager.py
├── models.py
├── working.py
├── sqlite_store.py
├── semantic_store.py
├── retrieval.py
├── workflow.py
└── privacy.py

actions/
├── __init__.py
├── models.py
├── registry.py
├── router.py
├── executor.py
├── browser.py
├── windows.py
└── screen.py

tests/
├── memory/
│   ├── test_models.py
│   ├── test_sqlite_store.py
│   ├── test_retrieval.py
│   └── test_workflows.py
│
└── actions/
    ├── test_models.py
    ├── test_registry.py
    ├── test_executor.py
    └── test_adapters.py
```

Adapt this structure to the existing repository rather than blindly creating duplicate architecture.

---

# 7. MEMORY BLOCK

# 7.1 Memory Categories

Memory should be divided conceptually into:

```text
Memory
├── Facts
├── Preferences
├── Episodes
└── Observations
```

These represent information about the user, environment, or previous interactions.

Do NOT treat workflows as ordinary text memories.

Workflows have their own structured model.

---

# 8. Memory Model

Every persistent memory should contain:

```text
Memory
├── id
├── type
├── content
├── source
├── confidence
├── created_at
├── updated_at
├── last_verified_at
├── expires_at
└── metadata
```

### Fields

### `id`

Unique identifier.

### `type`

Examples:

```text
fact
preference
episode
observation
```

### `content`

Structured or textual memory content.

Prefer structured data where appropriate.

### `source`

Possible sources:

```text
user
agent
system
execution
```

The source must distinguish explicitly provided information from inferred or observed information.

### `confidence`

Represent confidence in the memory.

Do not treat low-confidence inferred information as equivalent to explicit user-provided information.

### `created_at`

When the memory was created.

### `updated_at`

When the memory was last modified.

### `last_verified_at`

When the memory was last confirmed to still be valid.

### `expires_at`

Optional expiration timestamp.

This is important for agent systems because information can become stale.

For example:

```text
A workflow that worked six months ago
```

must not automatically be considered current.

### `metadata`

Additional structured information such as:

```text
tags
application
domain
execution_id
source_reference
verification_method
```

---

# 9. Memory Lifecycle

Memory must explicitly support lifecycle management.

```text
                 CREATED
                    |
                    v
                 ACTIVE
                    |
          +---------+---------+
          |                   |
          v                   v
       VERIFIED             STALE
          |                   |
          |                   v
          |                EXPIRED
          |                   |
          +--------+----------+
                   |
                   v
                INVALID
```

The exact state model can be implemented differently, but the system must be able to distinguish current information from potentially stale information.

At minimum, retrieval should consider:

* `confidence`
* `last_verified_at`
* `expires_at`
* current task relevance

A stale memory may still be returned as context, but it must not silently be presented as confirmed current truth.

---

# 10. Memory Verification

Memory can be verified through:

* explicit user confirmation
* successful execution
* current environment observation
* application state
* another trusted verification mechanism

When verification occurs:

```text
last_verified_at = current_time
```

Do not update verification timestamps merely because a memory was retrieved.

Retrieval is not verification.

---

# 11. Memory Privacy

The memory system must NOT automatically persist:

* Passwords
* OTPs
* PINs
* API keys
* Authentication tokens
* Session cookies
* Private keys
* Payment-card security information

Sensitive information encountered during execution should be filtered before persistent storage.

---

# 12. Working Memory

Working memory contains short-lived current-task information.

Examples:

```text
current task
current application
current browser page
recent observations
recent ActionResults
pending actions
current task state
```

Working memory may remain in RAM and does not automatically need persistent storage.

---

# 13. Persistent Memory

Persistent memory contains information intended to survive across tasks.

Examples:

```text
facts
preferences
episodes
observations
```

Persistent storage is backed by SQLite.

---

# 14. SQLite

SQLite is the primary source of truth for persistent memory metadata and records.

Use SQLite for:

* Memory records
* Metadata
* Lifecycle information
* Provenance
* Timestamps
* Workflow metadata
* Verification history references

Do not make LanceDB the source of truth.

---

# 15. SQLite FTS5

Use SQLite FTS5 for exact/keyword-oriented retrieval.

Useful for:

```text
application names
URLs
specific terms
IDs
exact terminology
names
```

Example:

```text
"JVVNL"
```

Exact retrieval can be more useful than semantic similarity.

---

# 16. LanceDB

Use LanceDB for semantic retrieval.

Use local embeddings.

Semantic retrieval should help with queries such as:

```text
"How did I download my electricity bill last time?"

"What's the workflow I used for my previous bill?"

"Which application did I use for this?"
```

---

# 17. Hybrid Retrieval

Use both:

```text
SQLite FTS5
+
LanceDB
```

Architecture:

```text
                   Query
                     |
          +----------+----------+
          |                     |
          v                     v
     SQLite FTS5            LanceDB
     exact search       semantic search
          |                     |
          +----------+----------+
                     |
                     v
                   Merge
                     |
                     v
                  Rerank
                     |
                     v
             Relevance Filter
                     |
                     v
              Context Builder
                     |
                     v
                  Planner
```

Do not simply concatenate all search results.

The retrieval layer should filter irrelevant results.

---

# 18. Retrieval Ranking

Ranking may consider:

* semantic similarity
* exact-match relevance
* memory type
* confidence
* recency
* verification status
* expiration
* task relevance

Do not over-engineer ranking in v1.

A simple deterministic ranking strategy is preferred initially.

---

# 19. Separate MEMORY and WORKFLOW

This is a critical architectural requirement.

Do NOT represent workflows merely as text blobs inside generic memories.

Use:

```text
Memory
├── facts
├── preferences
├── episodes
└── observations

Workflow
├── goal
├── preconditions
├── ordered steps
├── expected outcomes
└── verification history
```

A workflow is an executable knowledge structure.

A memory is information/context.

---

# 20. Workflow Model

Conceptually:

```text
Workflow
├── id
├── goal
├── preconditions
├── ordered_steps
├── expected_outcomes
├── failure_conditions
├── verification_history
├── source_execution_id
├── confidence
├── created_at
├── updated_at
├── last_verified_at
├── expires_at
└── version
```

The exact Pydantic schema may differ, but these concepts must be represented.

---

# 21. Workflow Preconditions

Before applying a stored workflow, the current environment should be checked against its preconditions.

Example:

```text
Workflow:
Download electricity bill

Preconditions:
- Browser available
- User is authenticated
- Billing website accessible
- Bills page available
```

The workflow must not blindly assume these conditions are true.

---

# 22. Workflow Steps

Steps should be structured.

Example:

```text
1. Open provider website
2. Navigate to billing section
3. Select latest bill
4. Click download
5. Verify downloaded file
```

Each step should be compatible with the Action Block.

---

# 23. Workflow Expected Outcomes

Each workflow step may specify an expected result.

Example:

```text
Step:
Click Download

Expected outcome:
A PDF download begins.
```

The overall workflow should have a final success condition.

Example:

```text
Final outcome:
PDF exists in the expected download location.
```

---

# 24. Workflow Verification History

A workflow should track whether it has continued to work.

Example:

```text
VerificationHistory
├── execution_id
├── timestamp
├── success
├── evidence
├── observed_changes
└── notes
```

This allows the system to distinguish:

```text
Worked once six months ago
```

from:

```text
Worked successfully yesterday
```

---

# 25. Workflow Expiration

A workflow may have:

```text
expires_at
```

or become stale based on its verification history.

An expired workflow can still be retrieved as historical context, but should not automatically be treated as a current verified procedure.

---

# 26. Workflow Learning

Do not automatically turn every failed execution into a reusable workflow.

Preferred lifecycle:

```text
Task
 |
 v
Execution
 |
 v
Verified success
 |
 v
Workflow extraction
 |
 v
Workflow stored
 |
 v
Future verification
 |
 v
Workflow updated/versioned
```

Failed attempts can be stored as episodes.

They should not automatically become recommended workflows.

---

# 27. MemoryManager

Other components must interact with memory through a high-level manager.

Conceptually:

```python
memory.search(query)
memory.save(memory)
memory.update(memory_id, data)
memory.forget(memory_id)

memory.get_workflow(task)
memory.save_workflow(workflow)
memory.update_workflow(workflow_id, data)
```

The exact signatures can be refined after inspecting the existing repository.

The important rule:

> Other components must NOT directly access SQLite or LanceDB.

---

# 28. ACTION BLOCK

# 28.1 Purpose

The Action Block executes validated computer interactions.

It must be:

* deterministic
* capability-based
* modular
* observable
* independently testable

---

# 29. Action Architecture

```text
                     Planner
                        |
                        v
                  Action Object
                        |
                        v
                Pydantic Validation
                        |
                        v
                  Action Router
                  /      |      \
                 /       |       \
                v        v        v
           Playwright   UIA    PyAutoGUI
                \        |        /
                 \       |       /
                  +------v------+
                         |
                         v
                   ActionResult
```

---

# 30. Action Adapters Must Be Independent

Adapters must contain **only computer-interaction logic**.

```text
PlaywrightAdapter
WindowsAdapter
VisualAdapter
```

They must NOT contain:

* business logic
* planning
* workflow decisions
* user preferences
* memory retrieval
* LLM reasoning
* application-specific business decisions

Their responsibility is only:

> Interact with the computer and report what happened.

---

# 31. PlaywrightAdapter

The Playwright adapter should know:

* how to open/navigate browser pages
* locate browser elements
* click
* type
* read
* download
* capture browser evidence

It should NOT know why the user is performing the action.

Example:

```text
Bad:

download_electricity_bill()

Good:

click(target="Download")
```

Business/workflow semantics belong outside the adapter.

---

# 32. WindowsAdapter

The Windows adapter should know:

* how to locate UI controls
* click controls
* type
* read UI information
* interact with Windows applications

It should not know the business purpose of the action.

---

# 33. VisualAdapter

PyAutoGUI is the fallback for cases where normal UI automation cannot access the target.

Possible operations:

```text
mouse movement
click
typing
keyboard shortcuts
```

Coordinate-based interaction should be treated as fragile.

Where possible, capture evidence before/after important visual interactions.

---

# 34. Initial Action Capability Set

Start with:

```text
open_app
open_url
click
type
press_key
hotkey
scroll
read
screenshot
wait
download
close_app
```

Do not build dozens of actions before there is a requirement.

---

# 35. Action Model

Every action should contain:

```text
Action
├── action_id
├── type
├── target
├── parameters
├── timeout
├── preconditions
├── postconditions / expected_outcomes
├── risk
└── retry_policy
```

Use Pydantic validation.

---

# 36. Action Preconditions

An action can specify what should be true before execution.

Example:

```text
Action:
click Download

Precondition:
Download button is visible
```

The executor may perform lightweight technical checks.

The larger verification system may perform more sophisticated checks.

---

# 37. Action Postconditions

Actions should be able to specify expected outcomes.

Example:

```text
Action:
click Download

Expected outcome:
PDF download begins.
```

The executor does not need to own the entire application's verification engine.

Its job is to:

1. Execute the action
2. Capture observations/evidence
3. Return enough information for higher-level verification

---

# 38. ActionResult

ActionResult must be richer than:

```text
success
output
error
```

Use:

```text
ActionResult
├── success
├── action_id
├── output
├── evidence
├── error
├── duration
└── retryable
```

Additional metadata may be included when useful.

---

# 39. Evidence

`evidence` should support structured execution evidence.

Examples:

```text
screenshot
URL
page title
UI element
download path
timestamp
observed text
```

Conceptually:

```text
Evidence
├── type
├── value
├── timestamp
└── metadata
```

Examples:

```text
type = screenshot
value = /evidence/action_123.png
```

```text
type = url
value = https://example.com/bills
```

```text
type = download_path
value = C:/Users/.../bill.pdf
```

Evidence should be returned to the higher-level system rather than interpreted as business truth by the adapter.

---

# 40. Action Duration

Record execution duration.

Example:

```text
duration_ms
```

or equivalent.

This helps with:

* debugging
* performance monitoring
* retry analysis
* observability

---

# 41. Retryable

ActionResult should indicate whether the failure is potentially retryable.

Example:

```text
retryable = true
```

for a transient browser timeout.

But:

```text
retryable = false
```

for a validation error or unavailable capability.

The Action Block should not blindly retry every failure.

---

# 42. Action Execution

Conceptually:

```python
result = await executor.execute(action)
```

Execution pipeline:

```text
Action
  |
  v
Validate
  |
  v
Check capability
  |
  v
Check technical preconditions
  |
  v
Route to adapter
  |
  v
Execute
  |
  v
Capture evidence
  |
  v
Collect observations
  |
  v
Return ActionResult
```

---

# 43. Action Registry

Only registered capabilities may execute.

Example:

```text
ActionRegistry
├── open_url
├── open_app
├── click
├── type
├── press_key
├── hotkey
├── scroll
├── read
├── screenshot
├── wait
├── download
└── close_app
```

The LLM must not be able to invoke arbitrary Python functions.

---

# 44. No Arbitrary Code Execution

Do NOT expose general-purpose capabilities such as:

```text
execute_python(code)
execute_shell(command)
execute_powershell(command)
```

to the planner/LLM.

If system commands are required later, they must be explicitly defined as narrow, validated capabilities with appropriate authorization.

---

# 45. Browser Automation Strategy

Preferred hierarchy:

```text
DOM/accessibility information
        ↓
semantic/stable locator
        ↓
Playwright
        ↓
visual fallback only if necessary
```

Do not default to screenshot coordinate clicking when DOM-based interaction is available.

---

# 46. Windows Automation Strategy

Preferred hierarchy:

```text
Windows UI Automation
        ↓
pywinauto
        ↓
PyAutoGUI fallback
```

Use actual controls where possible.

---

# 47. Timeouts

Every external interaction must have a bounded timeout.

Never allow indefinite waits.

Example:

```text
timeout = 30 seconds
```

The exact default can be configurable.

---

# 48. Retry Policy

Retries must be:

* limited
* explicit
* failure-aware

Example:

```text
max_attempts = 2
```

Do not automatically retry irreversible or potentially duplicated actions.

---

# 49. Error Model

Use structured errors.

Examples:

```text
UnsupportedActionError
ValidationError
TargetNotFoundError
TimeoutError
AdapterError
VerificationError
PermissionError
```

Return enough context for the planner to decide what to do next.

Avoid:

```text
"Something went wrong"
```

Prefer information such as:

```text
error_type
message
adapter
target
retryable
evidence
```

---

# 50. Verification Boundary

The Action Block is responsible for **execution observations**, not the entire application's verification policy.

Example:

```text
click Download
      |
      v
ActionResult
      |
      +-- screenshot
      +-- URL
      +-- observed element
      +-- download path
      +-- timestamp
      |
      v
Higher-level verifier
      |
      v
Task-level decision
```

This keeps the Action Block reusable.

---

# 51. Safety Boundary

The Action Block must not independently decide whether a high-impact action is appropriate.

Examples:

```text
send money
delete data
purchase something
send an email
submit an important form
```

The higher-level planner/safety/approval system should determine whether such an action is permitted.

The Action Block enforces:

* schema validation
* capability restrictions
* technical preconditions
* execution constraints

It does not become a second planner.

---

# 52. Memory + Action Integration

The two blocks integrate through the planner/orchestrator.

```text
                     Planner
                        |
                        v
                MemoryManager
                        |
                        v
                Relevant Context
                        |
                        v
                     Planner
                        |
                        v
                  Action Objects
                        |
                        v
                 ActionExecutor
                        |
                        v
                  ActionResult
                        |
                        v
               Higher-level verifier
                        |
                        v
                  Verified outcome
                        |
                        v
                MemoryManager
```

The Action Block does not directly query memory.

The Memory Block does not directly control the computer.

---

# 53. Example: First Execution

User:

```text
Download my electricity bill.
```

Flow:

```text
Planner
   |
   v
Memory.search("electricity bill download")
   |
   v
No current verified workflow
   |
   v
Planner creates actions
   |
   v
ActionExecutor
   |
   v
Playwright
   |
   v
Website interaction
   |
   v
Download
   |
   v
ActionResult
   |
   v
Higher-level verification
   |
   v
Verified success
   |
   v
Memory / Workflow storage
```

A successful execution may produce a procedural workflow.

---

# 54. Example: Subsequent Execution

```text
Planner
   |
   v
Memory.get_workflow("download electricity bill")
   |
   v
Workflow found
   |
   v
Check workflow freshness
   |
   +---- expired/stale ----> adapt/revalidate
   |
   +---- current ----------> use as starting point
   |
   v
Execute actions
   |
   v
ActionResult
   |
   v
Verify
   |
   v
Update workflow verification history
```

A workflow must never be blindly replayed simply because it succeeded in the past.

---

# 55. Configuration

Configuration should not be hard-coded.

Examples:

```text
SQLite path
LanceDB path
embedding model
browser settings
timeouts
download directory
evidence directory
logging level
```

Use environment/configuration files where appropriate.

Never commit secrets.

---

# 56. Testing Strategy

## Memory tests

Test:

* Pydantic validation
* memory creation
* memory updates
* memory deletion/forgetting
* lifecycle
* expiration
* verification timestamps
* confidence
* provenance
* exact search
* semantic search
* hybrid retrieval
* relevance filtering
* sensitive-data filtering
* workflow creation
* workflow retrieval
* workflow expiration
* workflow verification history

## Action tests

Test:

* Pydantic action validation
* registry
* unsupported capabilities
* routing
* preconditions
* postconditions/expected outcomes
* timeout
* retry policy
* structured errors
* ActionResult
* evidence
* duration
* retryable flag
* adapter isolation

Separate pure unit tests from real browser/Windows integration tests.

---

# 57. Development Order

## Phase 1 — Action Foundation

1. Inspect existing repository
2. Define Pydantic action models
3. Create action registry
4. Create ActionExecutor
5. Create adapter interface
6. Implement Playwright adapter
7. Implement Windows adapter
8. Implement visual fallback
9. Implement initial action set

## Phase 2 — Action Reliability

10. Implement ActionResult
11. Structured errors
12. Preconditions
13. Expected outcomes/postconditions
14. Timeouts
15. Limited retries
16. Evidence capture
17. Execution duration
18. Retryable classification

## Phase 3 — Memory Foundation

19. Define Pydantic Memory models
20. Implement lifecycle fields
21. SQLite persistence
22. MemoryManager
23. Working memory
24. SQLite FTS5

## Phase 4 — Semantic Memory

25. LanceDB
26. Local embeddings
27. Semantic retrieval
28. Hybrid retrieval
29. Relevance filtering

## Phase 5 — Workflow System

30. Define separate Workflow model
31. Preconditions
32. Ordered steps
33. Expected outcomes
34. Verification history
35. Workflow expiration
36. Workflow versioning
37. Workflow retrieval
38. Workflow updates after successful execution

## Phase 6 — Integration

39. Planner → Memory
40. Planner → Action
41. ActionResult → Planner
42. Verified outcome → Memory
43. Workflow learning
44. End-to-end tests

---

# 58. Definition of Done — Memory

Memory is complete when:

* Pydantic models exist
* Memory lifecycle exists
* Facts/preferences/episodes/observations are represented
* SQLite is the source of truth
* FTS5 search works
* LanceDB semantic search works
* Hybrid retrieval works
* Working memory works
* Provenance exists
* Confidence exists
* Verification timestamps exist
* Expiration exists
* Sensitive data is filtered
* Workflows are separate from generic memory
* Workflow verification history exists
* Workflow expiration/staleness exists
* Other application components use MemoryManager rather than storage directly
* Tests cover major behavior

---

# 59. Definition of Done — Action

Action is complete when:

* Actions are schema validated
* Only registered capabilities can execute
* Preconditions are represented
* Expected outcomes/postconditions are represented
* Playwright browser interaction works
* Windows UI Automation works
* PyAutoGUI fallback exists
* ActionResult is structured
* Evidence is captured
* Duration is recorded
* Retryable status is returned
* Errors are structured
* Timeouts exist
* Retries are limited
* Adapters contain no business logic
* Arbitrary code execution is not exposed
* Tests cover major behavior

---

# 60. Engineering Rules

## Rule 1 — Keep it modular

Memory and Action must be independently testable.

## Rule 2 — No unnecessary infrastructure

Do not add distributed infrastructure without a real requirement.

## Rule 3 — No direct storage access

Other components communicate with Memory through MemoryManager.

## Rule 4 — No direct computer access

Other components communicate with the computer through ActionExecutor.

## Rule 5 — No arbitrary code execution

The LLM must only use explicitly registered capabilities.

## Rule 6 — Memory is not automatically truth

Retrieved memory must be treated as context.

## Rule 7 — Retrieval is not verification

Finding a memory does not make it current.

## Rule 8 — Workflows are not text blobs

Workflows are structured, executable knowledge with verification history.

## Rule 9 — Old workflows can become stale

Use:

```text
last_verified_at
expires_at
verification_history
```

to manage freshness.

## Rule 10 — Adapters contain no business logic

Adapters only interact with the computer and report observations.

## Rule 11 — Execution is not task success

An action can succeed while the overall task fails.

Example:

```text
Click Download
      ↓
click succeeded
      ↓
file did not appear
      ↓
task failed
```

The higher-level system must be able to distinguish these states.

## Rule 12 — Don't blindly replay workflows

Always consider current application state and workflow freshness.

---

# 61. Final Architecture

```text
┌─────────────────────────────────────────────────────────┐
│                    LARGER AGENT SYSTEM                  │
│                                                         │
│       Planner / Agents / Safety / Verification          │
│                                                         │
└───────────────┬───────────────────────┬─────────────────┘
                │                       │
                v                       v
┌────────────────────────┐   ┌────────────────────────────┐
│      MEMORY BLOCK      │   │       ACTION BLOCK         │
│                        │   │                            │
│ MemoryManager          │   │ ActionExecutor             │
│                        │   │                            │
│ ┌────────────────────┐ │   │ ┌────────────────────────┐ │
│ │ Facts              │ │   │ │ Action Registry        │ │
│ │ Preferences        │ │   │ │ Validation             │ │
│ │ Episodes           │ │   │ │ Preconditions          │ │
│ │ Observations       │ │   │ │ Expected outcomes      │ │
│ └────────────────────┘ │   │ └────────────────────────┘ │
│                        │   │             │              │
│ ┌────────────────────┐ │   │      ┌──────┼──────┐       │
│ │ Workflow           │ │   │      v      v      v       │
│ │ Goal               │ │   │ Playwright UIA  Visual    │
│ │ Preconditions      │ │   │      │      │      │       │
│ │ Ordered Steps      │ │   │      └──────┼──────┘       │
│ │ Expected Outcomes  │ │   │             v              │
│ │ Verification Hist. │ │   │       ActionResult        │
│ └────────────────────┘ │   │             │              │
│                        │   │       Evidence             │
│ SQLite + FTS5          │   │       Duration             │
│ LanceDB                │   │       Retryable            │
│ Working Memory         │   │       Errors               │
└────────────────────────┘   └────────────────────────────┘
```

---

# 62. Claude Implementation Instructions

Before writing code:

1. Inspect the existing repository.
2. Identify current architecture.
3. Find existing memory/action-related code.
4. Identify existing dependencies.
5. Identify integration points with the planner.
6. Identify conflicts with this specification.
7. Do not immediately rewrite existing code.

First report:

```text
Existing relevant files:
...

Existing architecture:
...

Current dependencies:
...

Conflicts with specification:
...

Recommended implementation:
...

Files that will change:
...

New dependencies:
...

Tests to add:
...
```

Then implement incrementally.

---

# 63. Implementation Behavior

For every major phase:

```text
Inspect
  ↓
Implement
  ↓
Run tests
  ↓
Review failures
  ↓
Fix
  ↓
Report
  ↓
Continue
```

Do not perform a giant untested rewrite.

Do not modify unrelated application code.

Preserve existing interfaces where practical.

If an architectural conflict requires a significant change, explain the conflict before making the change.

---

# 64. Final Claude Report

At completion provide:

```text
## Implemented

- ...

## Files Changed

- ...

## New Files

- ...

## Dependencies Added

- ...

## Tests

- ...

## Integration API

Memory:
- ...

Action:
- ...

## Known Limitations

- ...

## Security Considerations

- ...

## Next Integration Step

- ...
```

The final implementation should be:

```text
modular
minimal
testable
observable
secure
replaceable
integration-friendly
```

and should not introduce unnecessary infrastructure or business logic into either block.

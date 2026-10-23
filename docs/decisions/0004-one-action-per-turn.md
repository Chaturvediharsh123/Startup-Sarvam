# 0004. One action per turn

- Status: Accepted
- Date: 2026-10-08

## Context

The LLM can return several tool calls for one sentence ("open Chrome and search
cricket"). Chained actions are hard to confirm, cancel and explain by voice, and one
misheard sentence could cause several side effects.

## Decision

- The brain returns **at most one tool call** per turn (`BrainResult.tool_call`). Extra
  calls from the LLM are dropped and logged.
- Multi-step requests are handled as a conversation: the assistant does the first step
  and the user says the next one. Memory resolves "isko" / "it" to the last action.

## Consequences

- Simpler safety: one confirmation, one rate-limit tick and one result phrase per turn.
- Compound commands take more turns. The golden set scores only the first expected action.
- Revisit for V2 if users often chain commands.

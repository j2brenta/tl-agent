# Phase 5 deep-dive — prompt evolution

The ReACT loop's system prompt. Opus 4.7 for the deepest reasoning.

## Versions

- `v1.md` (2026-05-22) — initial. Inline tool list (8 read-only tools),
  three diagnostic questions, three hard rules ("don't claim, ask",
  "stop after 8 tool calls", "don't repeat failed calls"). The "hard rules"
  section explicitly pairs with `check_success_claim` in the verifier.

## Open questions

- The tool list duplicates what Anthropic shows the model via the tools
  parameter. Worth keeping in the prompt? Today yes — being explicit
  about the search_standup_history syntax saved at least one regression.

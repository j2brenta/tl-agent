# Phase 2 triage — prompt evolution

Per-engineer triage. Per-call cost is the dominant factor (we run it 4x
in parallel every day), so Haiku is the default route.

## Versions

- `v1.md` (2026-05-22) — initial. Lists heuristics, constrains output to
  EngineerTriage Pydantic schema. No expected_tools_for_phrases yet (we
  rely on the deterministic claim-checker downstream).

## Open questions

- Should we surface the engineer's "baseline" inline in the prompt, or
  rely on the agent fetching `get_baseline`? Currently inline — fewer
  tokens, simpler prompt. Revisit if baselines bloat past a few lines.

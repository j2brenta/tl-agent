---
description: Run the tl-agent eval harness
allowed-tools: Bash
argument-hint: "[quick|full|<case_id>]"
---

# /eval — Run the eval harness

Usage:
- `/eval` — runs the quick suite (gates every commit)
- `/eval full` — runs every case (nightly)
- `/eval <case_id>` — runs one named case (e.g. `/eval 14_premature_stop_should_dig_more`)

## What this does

1. Sets `EVAL_LIVE=1` so the runner exercises the orchestrator against the
   compose stack (assumes `make up && make seed` has been run).
2. Calls `python -m evals.runner --suite $ARG`.
3. Renders the markdown report and saves it under `evals/reports/`.

## Acceptance

- Overall pass rate ≥ 85% on `full`.
- No single failure category drops below its threshold in
  `evals/failure_taxonomy.py::acceptance_thresholds()`.
- Zero-tolerance categories: `idempotency_violation`, `readback_failure`.

```
EVAL_LIVE=1 uv run python -m evals.runner --suite ${ARGUMENTS:-quick}
```

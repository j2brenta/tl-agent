---
description: Replay one eval case against a different prompt version
allowed-tools: Bash, Read
argument-hint: "<case_id> --prompt-version <N> [--phase <name>]"
---

# /replay — Re-run an LLM call from a saved trace with a different prompt

Loads the JSONL trace for `<case_id>` and re-issues just the LLM call(s)
for the named phase against `prompts/<phase>/v<N>.md`. Use this to answer:

> Would prompt v2 have caught the regression in case 14?

```
uv run python -m evals.replay $ARGUMENTS
```

The output is a side-by-side diff of the original vs. replayed verdict.
Wire to Anthropic by setting `TLA_ANTHROPIC_API_KEY` in the env first.

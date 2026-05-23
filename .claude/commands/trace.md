---
description: Reconstruct a tl-agent run from its JSONL trace
allowed-tools: Bash, Read
argument-hint: "<run_id_or_date>"
---

# /trace — Reconstruct a bad run

Given a date or a run id, find the JSONL trace under
`traces/YYYY-MM-DD/spans.jsonl` and surface:
- The phase span tree
- Every LLM call's tokens / cost / cache_hit
- Every tool error (status=ERROR)
- The verifier verdict per Phase 5 hot spot

This is the single fastest path to "why did this morning's brief look
weird". Phoenix UI is the same data with a tree view at
http://localhost:6006.

```
ls -lt traces/$ARGUMENTS/ 2>/dev/null || ls -lt traces/
echo ""
echo "==> use the JSONL exporter directly:"
echo "  jq 'select(.attributes[\"tl_agent.layer\"]==\"llm\")' traces/$ARGUMENTS/spans.jsonl"
echo "  jq 'select(.status==\"ERROR\")' traces/$ARGUMENTS/spans.jsonl"
```

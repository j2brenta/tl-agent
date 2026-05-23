---
description: Seed the compose stack (GitLab + Jira mock + Mattermost + SQLite)
allowed-tools: Bash
---

# /seed — Bring up a clean, deterministic dev state

Re-seeds GitLab, jira_mock, Mattermost, and the local SQLite. Idempotent.

```
make up
make seed
make snapshot
```

After this, `/eval quick` should pass and `make run` produces a brief.

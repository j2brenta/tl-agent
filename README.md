# tl-agent — Tech-Lead Agentic Workflow

A production-grade agent that runs an 8-phase morning loop for a tech lead over
a team of four (John, Matt, Alicia, Karen): collects Jira/GitLab/standup
signals, triages each engineer, cross-correlates, deep-dives uncertain hot
spots with a ReACT loop, picks a response mode, drafts artifacts, and requires
TL approval before any side-effect lands.

This README documents the **tradeoffs** — what we chose not to build, and why.
For the feature list, read the code; for how to run it, see the Makefile.

> Status: early. See `make help` for what works today.

---

## Tradeoffs — what we did NOT build, and why

### No LangChain / CrewAI / AutoGen wrapper
The control loop is in `src/tl_agent/agent/loop.py` — about 200 lines.
Wrapper frameworks hide the layer that matters when an agent misbehaves
(stop conditions, verifier feedback, tool-arg validation), and "we built it on
LangChain" is not a credible answer to "tell me about a time the agent
confidently lied to you." We accept the cost of writing our own scaffolding so
the loop is debuggable.

### No vector store
The corpus is 4 engineers × 7 days of standups + a sprint's worth of tickets.
SQLite FTS5 covers `search_standup_history` in <1 ms per query with zero extra
infra. If the team grows past ~20 engineers or the standup window past a
quarter, swap in pgvector behind the `memory.search_standup_history` tool —
the interface won't change.

### No multi-tenancy
Single-team scope by design. The data model assumes one TL, one team. Adding
tenants means a `tenant_id` column on every table + a tenant filter in every
query — explicit work, not free.

### Mattermost is primary, Slack is a stub adapter
Both implement the `ChatProvider` ABC in `src/tl_agent/tools/chat/base.py`.
Mattermost runs in `docker compose` so every PR exercises real REST, real
webhook signature verification, and real OAuth refresh — the senior-signal
parts. Slack uses the official SDK and is wired but not test-gated (we don't
seed a real workspace in CI). To flip primary: set `provider: slack` in
`config/chat.yaml`, set `SLACK_BOT_TOKEN`, drop the `skip-integration` marker
on the chat tests. ~half a day.

### Mock Jira, real GitLab
The official Jira docker image is ~6 GB and takes 10–15 minutes to first-boot,
which kills the eval-reset loop. GitLab CE boots in ~90 seconds and resets
cleanly via API. So we mock the painful one and run the easy one for real.
The Jira mock in `services/jira_mock/` speaks the REST subset we actually use;
the shape of the calls matches the real API so swapping is one config change.

### No subprocess sandboxing for tool execution
No tool runs user-supplied code — every tool is a typed Pydantic function
that calls a documented HTTP API or a SQLite query. The blast radius is bounded
by the tool registry. We DO gate writer tools (`requires_approval=True`) so
Phase 5 (ReACT) cannot bind them at all — defense in depth. If you add a
`run_python` tool later, see `src/tl_agent/tools/base.py` for the seam.

### No real OAuth flow against Slack
But Mattermost exercises the full refresh-on-expiry path against a real OAuth
server in compose — the abstraction is actually tested, not theatre.

### One model per phase, not one model everywhere
`config/router.yaml` maps `(phase, tier) → provider:model`. Phase 2 triage
runs on Haiku 4.5 (cheap, per-engineer parallel); Phase 5 ReACT runs on Opus
4.7 (the only phase that needs the deepest reasoning). Two providers
(Anthropic, Ollama) ship behind the same `Provider` ABC. Single-model
hardcoding is a red flag we explicitly avoid.

---

## What's in here

| Directory | Purpose |
|-----------|---------|
| `src/tl_agent/` | The agent package: models, storage, tools, llm router, agent loop, phases, web UI, observability |
| `prompts/` | Version-controlled, per-phase prompts with provenance frontmatter |
| `config/` | LAYER 1 — flat markdown loaded fresh each run |
| `services/jira_mock/` | FastAPI Jira REST subset + deterministic fixtures |
| `services/mattermost_seed/` | Seeds the real Mattermost server + receives outgoing webhooks |
| `infra/` | docker-compose, seed scripts, snapshot/restore for evals |
| `evals/` | 20+ scenarios + failure taxonomy + replay tool |
| `tests/` | unit + integration + golden snapshot |
| `.claude/` | hooks, slash commands, subagents — build for self |
| `mcp/` | MCP server so Claude Code drives our own iteration |

---

## Quick start

```bash
make install            # uv sync
make up && make seed    # compose up + seed gitlab/mattermost/jira_mock
make snapshot           # capture post-seed baseline for evals
make check              # ruff + mypy strict + pyright + unit tests
make eval-quick         # 5 hand-curated scenarios
make run                # tech-lead loop for today
make web                # review UI on :8080
make trace              # Phoenix span viewer
```

See `make help` for the full list.

---

## "Tell me about a time the agent confidently lied"

`src/tl_agent/agent/verifier.py` — the success-claim-without-tool-call detector.
Eval case `evals/cases/14_premature_stop_should_dig_more.yaml` is the
regression that drove building it. The Phoenix trace UI lets you replay any
specific run; the JSONL sink under `traces/YYYY-MM-DD/` makes diffs reviewable
in git.

# tl-agent — Tech-Lead Agentic Workflow

A production-grade agent that runs an 8-phase morning loop for a tech lead
over a team of four (John, Matt, Alicia, Karen): collects Jira/GitLab/standup
signals, triages each engineer, cross-correlates, deep-dives uncertain hot
spots with a ReACT loop, picks a response mode, drafts artifacts, and
requires TL approval before any side-effect lands.

This README documents the **tradeoffs** — what we chose not to build and why.
For the feature list, read the code; for how to run it, see the Makefile.

> Status: end-to-end skeleton runnable. 105 unit tests green; integration tests
> against the compose stack are stubbed but not yet wired into CI.

---

## Quick start

```bash
make install               # uv sync --all-extras
make up && make seed       # compose up + seed gitlab/mattermost/jira_mock
make snapshot              # capture post-seed baseline for evals
make check                 # ruff + mypy strict + pyright + unit tests
make eval-quick            # 5 hand-curated scenarios (no API key needed)
make run                   # tech-lead loop for today (needs ANTHROPIC_API_KEY)
make web                   # review UI on :8080
make trace                 # Phoenix span viewer on :6006
```

See `make help` for everything.

---

## Tradeoffs — what we did NOT build, and why

### No LangChain / CrewAI / AutoGen wrapper

The control loop is `src/tl_agent/agent/loop.py` — about 250 lines, explicit
`step()` / `stop()`, stop reasons as a named enum. Wrapper frameworks hide
the layer that matters when an agent misbehaves (stop conditions, verifier
feedback, tool-arg validation). "We built it on LangChain" is not a credible
answer to "tell me about a time the agent confidently lied." We accept the
cost of writing our own scaffolding so the loop is debuggable.

### No vector store

The corpus is 4 engineers × 7 days of standups + a sprint's worth of tickets.
SQLite FTS5 covers `search_standup_history` in sub-millisecond per query with
zero extra infra. If the team grows past ~20 engineers or the standup window
past a quarter, swap in pgvector behind the `tools.memory.SearchStandupHistoryTool`
— the interface won't change.

### No multi-tenancy

Single-team scope by design. The data model assumes one TL, one team. Adding
tenants means a `tenant_id` column on every table plus a tenant filter in
every query — explicit work, not free.

### Mattermost is primary, Slack is a stub adapter

Both implement the `ChatProvider` ABC in `src/tl_agent/tools/chat/base.py`.
Mattermost runs in `docker compose` so every PR exercises real REST, real
webhook signature verification (`services/mattermost_seed/webhook_target.py`),
and real OAuth refresh — the senior-signal parts. Slack uses the official
SDK and is wired but not test-gated (we don't seed a real workspace in CI).
To flip primary: set `provider: slack` in `config/chat.yaml`, set
`SLACK_BOT_TOKEN`, drop the `skip-integration` marker on the chat tests.
~half a day.

### Mock Jira, real GitLab

The official Jira docker image is ~6 GB and takes 10–15 minutes to first-boot,
which kills the eval-reset loop. GitLab CE boots in ~90 seconds and resets
cleanly. So we mock the painful one (`services/jira_mock/`, FastAPI) and run
the easy one for real. The mock speaks the REST subset we actually use; the
shape of the calls matches the real API so swapping is one config change.

### No subprocess sandboxing for tool execution

No tool runs user-supplied code — every tool is a typed Pydantic function
that calls a documented HTTP API or a SQLite query. The blast radius is
bounded by the tool registry. We DO gate writer tools (`requires_approval=True`)
so Phase 5 (ReACT) cannot bind them at all — defense in depth. If you add a
`run_python` tool later, see `src/tl_agent/tools/base.py` for the seam.

### No real OAuth flow against Slack

But Mattermost exercises the full refresh-on-expiry path against a real
OAuth server in compose. The abstraction is actually tested, not theatre.

### One model per phase, not one model everywhere

`config/router.yaml` maps `(phase, tier) → provider:model`. Phase 2 triage
runs on Haiku 4.5 (cheap, per-engineer parallel); Phase 5 ReACT runs on Opus
4.7 (the only phase that needs the deepest reasoning). Two providers
(Anthropic, Ollama) ship behind the same `Provider` ABC. Single-model
hardcoding is a red flag we explicitly avoid.

### No SPA for the review UI

FastAPI + HTMX. Phase 8 is a 4-button surface — a React app would be overkill
and would hide the markup. Templates are in `src/tl_agent/web/templates/`,
loaded via Jinja2, swapped inline via HTMX.

### Idempotency via SQLite, not Redis

The dedup load is one row per writer call per day. SQLite covers it; adding
Redis would mean an extra service to run, more network hops, and a new
failure mode. If you outgrow it, the swap is behind `IdempotencyLookup` in
`src/tl_agent/tools/base.py`.

---

## What's in here

| Directory                | Purpose                                                                                                |
|--------------------------|--------------------------------------------------------------------------------------------------------|
| `src/tl_agent/`          | The package: models, storage, tools, llm router, agent loop, phases, web UI, observability             |
| `prompts/`               | Version-controlled per-phase prompts with provenance frontmatter; READMEs are evolution logs          |
| `config/`                | LAYER 1 — flat markdown loaded fresh each run (team, ownership, escalation, tl_preferences, router)   |
| `services/jira_mock/`    | FastAPI Jira REST subset + deterministic fixtures                                                      |
| `services/mattermost_seed/` | Seeds the real Mattermost server + receives outgoing webhooks                                      |
| `infra/`                 | docker-compose, seed scripts, snapshot/restore for evals                                              |
| `evals/`                 | Failure taxonomy + runner + 7 representative cases + replay scaffold                                  |
| `tests/`                 | 105 unit tests; `integration/` is reserved for compose-backed runs                                    |
| `.claude/`               | Hooks (pre-commit make check), slash commands, subagents — build for self                             |
| `mcp/`                   | MCP server so Claude Code can drive `run_evals` / `get_trace` / `list_hotspots` itself                |
| `traces/`                | JSONL span dump, one file per UTC date — grep-able alternative to Phoenix                             |

---

## Test plan

```
make check
```

runs every gate the repo enforces:

- **ruff format --check** + **ruff check** — 30+ rule families enabled (E, F, W, I, B, UP, SIM, RUF, ASYNC, S, TID, PIE, PERF)
- **mypy --strict** with the Pydantic plugin — `disallow_untyped_defs`, `disallow_any_generics`, `warn_return_any`, `warn_unreachable`
- **pyright** strict mode — 0 errors today (29 third-party warnings from typer + slack-sdk stubs)
- **pytest** — 105 unit tests covering models, storage (incl. FTS5), tool registry / retry / idempotency / readback, LLM router + budget, OTel + JSONL + RunMetrics, stop conditions + verifier + agent loop, every Jira/GitLab tool, both chat providers + signature verification + OAuth refresh, every prompt loads, every eval case parses

Integration tests against the compose stack live under `tests/integration/`
and are gated behind `EVAL_LIVE=1` + a running `make up`. CI runs the
unit suite; integration runs nightly.

---

## "Tell me about a time the agent confidently lied"

`src/tl_agent/agent/verifier.py` — the success-claim-without-tool-call
detector. Eval case `evals/cases/14_premature_stop_should_dig_more.yaml` is
the canonical regression. The agent claimed it had identified a blocker but
the tool transcript showed it never called `get_ticket_dependencies`. The
verifier catches this by cross-checking the success-claim against the last
N tool calls; on mismatch it forces a `RETRY_WITH_HINT`. The fix landed
alongside `prompts/phase5_deepdive/v1.md`'s "hard rules" section.

Reconstruct any run from `traces/YYYY-MM-DD/spans.jsonl` (grep-friendly) or
the Phoenix UI at `localhost:6006`.

---

## License

MIT.

# tl-agent — Team Lead Agentic Workflow

A production-grade agent that runs an 8-phase morning loop for a team lead
over a team of four (John, Matt, Alicia, Karen): collects Jira/GitLab/standup
signals, triages each engineer, cross-correlates, deep-dives uncertain hot
spots with a ReACT loop, picks a response mode, drafts artifacts, and
requires TL approval before any side-effect lands.

This README documents the **tradeoffs** — what we chose not to build and why.
For the feature list, read the code; for how to run it, see the Makefile.

---

## Quick start

```bash
cp .env.example .env
# edit .env — set TLA_ANTHROPIC_API_KEY

make demo                   # up + seed + run + open links (default date)
make demo DATE=2026-06-01   # any date
make demo-fresh             # wipe state first, then demo
```

`make demo` does everything automatically: brings the compose stack up,
provisions all service tokens (GitLab PAT, Mattermost PAT), seeds fixtures,
posts a realistic standup transcript, runs the 8-phase pipeline, then prints
links to the web UI, audit log, sprint view, and Phoenix traces.

See `make help` for individual targets (`make check`, `make eval-quick`,
`make web`, `make seed`, `make reset-state`, etc.).

---

## Service tokens for `.env`

`make env` copies `.env.example` → `.env`. Everything is namespaced `TLA_*`.
Pydantic-settings reads it at startup; the bash seed scripts source it too,
so there's one source of truth.

The only required token to talk to a real LLM is `TLA_ANTHROPIC_API_KEY`.
Everything else has a sensible local default or is generated automatically
by `make demo` / `make seed`.

### `TLA_ANTHROPIC_API_KEY` — required for `make demo` / `make run`

1. https://console.anthropic.com/settings/keys → **Create Key**.
2. Paste into `.env`. No scopes to pick; billing must be enabled on the workspace.
3. Not needed for `make check` or `make eval-quick` (the quick suite uses fixtures, not the live API).

### `TLA_MATTERMOST_TOKEN` — provisioned automatically

`make demo` / `make seed` creates the admin user, issues a Personal Access
Token (long-lived, never expires), and writes it straight to `.env`.
Re-running seed revokes the old PAT and issues a fresh one.

### `TLA_GITLAB_TOKEN` + `TLA_GITLAB_ADMIN_TOKEN` — provisioned automatically

On the first run, `infra/gitlab/seed.sh` detects that the placeholder token
is invalid, provisions a PAT via `gitlab-rails runner` inside the container,
and writes it to both variables in `.env`. Subsequent runs pass the health
check and skip provisioning.

### `TLA_JIRA_TOKEN` — mock, `dev-token` works

`services/jira_mock/` is a FastAPI stub that accepts any non-empty bearer.
Leave the default unless you point `TLA_JIRA_BASE_URL` at a real Jira; in
that case create an API token at
https://id.atlassian.com/manage-profile/security/api-tokens and use
`email:token` base64-encoded per Atlassian's Basic-auth convention.

### `TLA_SLACK_BOT_TOKEN` + `TLA_SLACK_SIGNING_SECRET` — only if you flip the provider

Default chat provider is Mattermost. To switch, set
`TLA_CHAT_PROVIDER=slack` and:

1. https://api.slack.com/apps → **Create New App** → *From scratch*.
2. **OAuth & Permissions** → Bot Token Scopes: `chat:write`, `channels:history`,
   `channels:read`, `users:read`. **Install to Workspace** → copy the
   `xoxb-…` token → `TLA_SLACK_BOT_TOKEN`.
3. **Basic Information** → **Signing Secret** → `TLA_SLACK_SIGNING_SECRET`.

### Local-only knobs (no token to fetch)

- `TLA_OLLAMA_BASE_URL` + `TLA_OLLAMA_MODEL` — only consulted when a route picks `provider: ollama`. Default model is `qwen3:8b`; install Ollama locally and `ollama pull qwen3:8b` before first use.
- `TLA_ROUTER_CONFIG` — pick which router YAML to load. Empty → `config/router.yaml` (Anthropic). Set to `config/router.ollama.yaml` to run every phase on local `qwen3:8b` without touching the default router. No code or YAML edits required to flip back.
- `TLA_OTLP_ENDPOINT` — Phoenix runs in compose; set empty to disable remote tracing.
- `TLA_GITLAB_IMAGE` — uncomment on Apple Silicon to use the community arm64 build.

---

## Tradeoffs — what was NOT built, and why

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
| `evals/`                 | Failure taxonomy + runner + eval cases + replay scaffold                                              |
| `tests/`                 | Unit tests; `integration/` is reserved for compose-backed runs                                        |
| `.claude/`               | Hooks (pre-commit make check), slash commands, subagents — build for self                             |
| `mcp/`                   | MCP server so Claude Code can drive `run_evals` / `get_trace` / `list_hotspots` itself                |
| `traces/`                | JSONL span dump, one file per UTC date — grep-able alternative to Phoenix                             |

---

## Running a test end-to-end

The fastest way to exercise the full 8-phase loop against the real compose
stack:

```bash
make demo
```

Everything — stack, tokens, seed data, standup transcript, pipeline run —
happens in one command. Links to every viewing surface are printed at the end.

To exercise a specific date or start clean:

```bash
make demo DATE=2026-05-25
make demo-fresh              # wipes SQLite state before running
```

### What's in the seeded setup

After `make demo` / `make seed`, the following exists:

- **Team** — `john`, `matt`, `alicia`, `karen` in the `engineering` team.
- **Mattermost** at `http://localhost:8065` — channel `town-square` contains
  7 days of standups (2026-05-16 → 2026-05-22) for all four engineers, plus
  the day's standup posted by `make demo`.
- **Jira mock** at `http://localhost:9100` — one active sprint with tickets
  `ENG-1`..`ENG-N` assigned across the four engineers, including a
  deliberate cross-engineer blocker (`ENG-9` blocks `ENG-12`) and an
  off-sprint commit scenario.
- **GitLab CE** at `http://localhost:8929` — one project with commits on
  feature branches referencing sprint tickets.
- **SQLite** at `data/tl_agent.db` — schema applied, `engineer_baselines`
  populated so Phase 2 doesn't flag baseline behaviours as anomalies.
- **Phoenix** at `http://localhost:6006` — receives OTel spans from every
  phase / tool / LLM call as the run executes.

### Inspect after the run

```bash
sqlite3 data/tl_agent.db "select date, type, status, days_hot from daily_flags order by date desc limit 20;"
sqlite3 data/tl_agent.db "select date, proposed_mode, hotspot_id, substr(proposed_body,1,80) from decisions order by created_at desc;"
open http://localhost:6006   # Phoenix — per-phase spans, token counts, latencies
```

### Reset between runs

```bash
make reset-state   # wipe SQLite, re-apply schema, re-seed
make nuke          # also wipe Mattermost/GitLab volumes
```

---

## Test plan

```
make check
```

runs every gate the repo enforces:

- **ruff format --check** + **ruff check** — 30+ rule families enabled (E, F, W, I, B, UP, SIM, RUF, ASYNC, S, TID, PIE, PERF)
- **mypy --strict** with the Pydantic plugin — `disallow_untyped_defs`, `disallow_any_generics`, `warn_return_any`, `warn_unreachable`
- **pyright** strict mode
- **pytest** — unit tests covering models, storage (incl. FTS5), tool registry / retry / idempotency / readback, LLM router + budget, OTel + JSONL + RunMetrics, stop conditions + verifier + agent loop, every Jira/GitLab tool, both chat providers + signature verification + OAuth refresh, every prompt loads, every eval case parses

Integration tests against the compose stack live under `tests/integration/`
and are gated behind `EVAL_LIVE=1` + a running `make up`.

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

MIT. See [LICENSE](LICENSE)

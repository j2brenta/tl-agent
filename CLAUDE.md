# CLAUDE.md — orientation for Claude Code

This file is loaded into your context whenever you work in this repo. Read
it before doing anything substantive. Update it when something here becomes
stale.

## What this is

`tl-agent` is a tech-lead agentic workflow: an 8-phase morning loop over
four engineers (John, Matt, Alicia, Karen). The approved plan is
`/Users/kirill/.claude/plans/let-s-build-the-tech-staged-stonebraker.md`;
the user-facing summary lives in `README.md`.

This is a **hiring-bar portfolio artifact**. Every architectural choice
should defend against the senior-agent-build checklist (eval harness,
traces, idempotency, tool registry, model router, prompt provenance,
`.claude/` hygiene, README of tradeoffs). When in doubt, pick the option
that gives the stronger senior signal even if it's more work.

## File map (where bodies are buried)

- **Domain models:** `src/tl_agent/models/` — every contract is Pydantic;
  changes here ripple everywhere; bump tests in `tests/unit/test_models.py`
- **Storage:** `src/tl_agent/storage/{schema.sql, db.py, repos/*}` —
  8 SQLite tables, FTS5 on standup_observations, transactions via the
  `transaction()` ctx manager
- **Working context / memory tiers:** `src/tl_agent/storage/working_context.py`
- **Markdown config (LAYER 1):** `config/{team,ownership,escalation,tl_preferences,router}.md|yaml`
- **Tool contract:** `src/tl_agent/tools/base.py` — every tool subclasses
  `BaseTool[InputT, OutputT]`, declares `idempotency_key`, `retry_policy`,
  and `requires_approval`
- **Tool registry:** `src/tl_agent/tools/registry.py` — `for_phase5()`
  enforces writer exclusion
- **Idempotency + readback:** `src/tl_agent/tools/{idempotency,readback}.py`
- **Concrete tools:**
  - `tools/jira.py` (get_ticket, get_history, get_dependencies, list_sprint, post_comment)
  - `tools/gitlab.py` (list_commits, get_commit_diff, list_branches)
  - `tools/chat/{base,mattermost,slack,factory,tools}.py`
  - `tools/memory.py` (search_standup_history via FTS5, get_baseline)
- **LLM router:** `src/tl_agent/llm/{base,router,anthropic_provider,ollama_provider,budget,prompts}.py`
- **Observability:** `src/tl_agent/obs/{tracing,spans,jsonl_sink,metrics}.py`
- **The ReACT loop:** `src/tl_agent/agent/loop.py`
- **The verifier:** `src/tl_agent/agent/verifier.py` — re-reads artifact +
  tool transcript; the `check_success_claim` detector is the "confidently
  lied" guard
- **Stop conditions:** `src/tl_agent/agent/stop_conditions.py` — named enum
- **Phases:** `src/tl_agent/phases/{orchestrator, phase0…phase8}.py`
- **Prompts (versioned):** `prompts/<phase>/v1.md` + per-phase READMEs
  (the evolution log)
- **Web UI:** `src/tl_agent/web/{app,routes,templates}/*` — FastAPI + HTMX
- **Eval harness:** `evals/{failure_taxonomy,runner,replay,cases/*.yaml}`
- **Compose stack:** `infra/docker-compose.yml`, `services/jira_mock/`,
  `services/mattermost_seed/`, `infra/{gitlab,mattermost}/`
- **MCP server:** `mcp/server.py` (optional extra dep; excluded from strict
  type-check for that reason)

## How to work here

- **`make check` is the gate.** Run before every commit. Hooks in
  `.claude/hooks/pre_commit.sh` enforce this.
- **`make eval-quick`** runs 5 scenarios in <60s. Run before merging
  anything touching phases, prompts, or tools.
- **New tools subclass `BaseTool`.** Define Pydantic input/output models,
  declare `idempotency_key` (writers only), set `requires_approval`. The
  registry validates at import time — schema errors fail loudly.
- **New prompts get a new version file** (`vN+1.md`) with frontmatter
  recording `parent_version` and `change_reason`. Never edit a vN.md in
  place — that breaks reproducibility of past runs.
- **New phases get an OTel span via `@phase_span`.** New tools get
  `@tool_span` (via `BaseTool.invoke`). All LLM calls go through the
  router which emits `llm_span`.
- **Writer tools (`requires_approval=True`) cannot be bound to Phase 5's
  ReACT loop.** The registry enforces this — do not bypass it.

## The "tell me about a time the agent confidently lied" answer

Eval case `evals/cases/14_premature_stop_should_dig_more.yaml` is the
canonical regression. The agent claimed it had identified a blocker but the
tool transcript showed it never called `get_ticket_dependencies`. The
verifier in `src/tl_agent/agent/verifier.py::check_success_claim` catches
this by cross-checking the success-claim against the last N tool calls;
on mismatch it forces a `RETRY_WITH_HINT`. The fix landed alongside
`prompts/phase5_deepdive/v1.md`'s "hard rules" section.

The full trace for any run lives in `traces/YYYY-MM-DD/spans.jsonl` and
the Phoenix UI at `localhost:6006`.

## Conventions

- Imports: stdlib → third-party → local; sorted by ruff.
- Strings: double quotes; f-strings preferred over `.format`.
- Async: prefer `anyio` over raw asyncio when both work; `asyncio` is fine when needed.
- Logging: structured via `logger.info("event", extra={"k": v})`.
- Comments: rare. Identifier names carry the WHAT; comments explain WHY when non-obvious.

## Things to avoid

- LangChain / CrewAI / AutoGen — see README.md for the rationale.
- Bypassing the registry to call a tool directly — defeats validation, retry, idempotency, audit.
- Editing prompt files in place — always bump the version.
- Hardcoding a model name outside `config/router.yaml` — use `router.for_phase(phase)`.
- Mocking the database in tests — use the real SQLite, in tmp dir. Mock the network instead.
- Adding deps without thought — `pyproject.toml` already covers the essentials; new deps need a sentence of justification in the commit.

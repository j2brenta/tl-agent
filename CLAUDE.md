# CLAUDE.md — orientation for Claude Code

This file is loaded into your context whenever you work in this repo. Read it
before doing anything substantive. Update it when something here becomes stale.

## What this is

`tl-agent` is a tech-lead agentic workflow: an 8-phase morning loop over four
engineers (John, Matt, Alicia, Karen). Phases are described in the approved
plan at `/Users/kirill/.claude/plans/let-s-build-the-tech-staged-stonebraker.md`
and summarised in `README.md`.

This codebase is a **hiring-bar portfolio artifact** — every architectural
choice should defend against the senior-agent-build checklist (eval harness,
traces, idempotency, tool registry, model router, prompt provenance,
`.claude/` hygiene, README of tradeoffs). When in doubt, pick the option that
gives the stronger senior signal even if it's more work.

## File map (where bodies are buried)

- **The loop:** `src/tl_agent/agent/loop.py` — ReACT, explicit step/stop, NOT `while True`
- **The verifier:** `src/tl_agent/agent/verifier.py` — re-reads artifact + tool transcript; success-claim-without-action detector
- **Tool contract:** `src/tl_agent/tools/base.py` — every tool is Pydantic-validated, idempotent, structured-error-returning
- **Tool registry:** `src/tl_agent/tools/registry.py` — gates writers from Phase 5
- **Model router:** `src/tl_agent/llm/router.py` — Anthropic + Ollama behind one ABC
- **Orchestrator:** `src/tl_agent/phases/orchestrator.py` — wires P0→P8 with OTel spans
- **Storage schema:** `src/tl_agent/storage/schema.sql` — the 6 spec tables + FTS5
- **Eval harness:** `evals/runner.py` + `evals/failure_taxonomy.py`
- **Prompts:** `prompts/<phase>/vN.md` — frontmatter has provenance; READMEs are evolution logs
- **Chat abstraction:** `src/tl_agent/tools/chat/base.py` — Mattermost primary, Slack stub

## How to work here

- `make check` is the gate. Run before every commit. It runs ruff (format + lint), mypy strict, pyright strict, and unit tests.
- `make eval-quick` runs 5 scenarios in <60s. Run before merging anything touching phases, prompts, or tools.
- All new tools subclass `BaseTool`. Define Pydantic input/output models, an `idempotency_key` (writers only), and `requires_approval`. The registry validates at import time — schema errors fail loudly.
- All new prompts get a new version file (`vN+1.md`) with frontmatter recording `parent_version` and `change_reason`. Never edit a vN.md in place — that breaks reproducibility of past runs.
- All new phases get an OTel span via `@phase_span`. All new tools get `@tool_span`. All LLM calls go through the router (which emits `@llm_span`).
- Writer tools (`requires_approval=True`) cannot be bound to Phase 5's ReACT loop. The registry enforces this — do not bypass it.

## The "tell me about a time the agent confidently lied" answer

Eval case `evals/cases/14_premature_stop_should_dig_more.yaml` is the canonical
regression. The agent claimed it had identified a blocker but the tool
transcript showed it never called `get_ticket_dependencies`. The verifier in
`src/tl_agent/agent/verifier.py` catches this by cross-checking the
success-claim against the last N tool calls; on mismatch it forces a
`RETRY_WITH_HINT`. The fix landed alongside `prompts/phase5_deepdive/v2.md` —
see that file's `change_reason` frontmatter.

## Conventions

- Imports: stdlib → third-party → local; sorted by ruff.
- Strings: double quotes; f-strings preferred over `.format`.
- Async: prefer `anyio` over raw asyncio when both work; `asyncio` is fine when needed.
- Logging: structured via `structlog` if added; until then, `logger.info("event", k=v)` style.
- Comments: rarely. Identifier names should carry the WHAT; comments are for the WHY when non-obvious.

## Things to avoid

- LangChain / CrewAI / AutoGen — see README.md for the rationale.
- Bypassing the registry to call a tool directly — defeats validation, retry, idempotency, audit.
- Editing prompt files in place — always bump the version.
- Hardcoding a model name outside `config/router.yaml` — use `router.for_phase(phase, tier)`.
- Mocking the database in tests — use the real SQLite, in tmp dir. Mock the network instead.

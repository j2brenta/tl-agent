# Findings — surprises and decisions from building tl-agent

Two categories: **runtime/operational** surprises (vendor APIs, infra) and
**build-time/static-analysis** surprises (type system, linters, SDK quirks).
Sourced from commit bodies, prompt-evolution READMEs, and the initial build
session transcript — not impressions.

---

## Runtime / operational

### Anthropic API
- Claude 4.x **rejects the `temperature` parameter** (HTTP 400 "deprecated for
  this model"). Required a prefix-matcher (`claude-opus-4`, `claude-sonnet-4`,
  `claude-haiku-4`) so future point releases are picked up automatically. 3.x
  still accepts it. See `aa79b44` and `src/tl_agent/llm/anthropic_provider.py`.

### GitLab idiosyncrasies
- **GitLab Files API ignores `author_date`** and always stamps
  `committed_date = wall-clock now`. So seeded commits land outside the agent's
  fetch window unless re-seeded at demo time. Drove the `--anchor-date`
  workaround (`9d7f5a7`).
- Project paths **must be URL-encoded** (`tl-agent%2Fdemo`) or GitLab treats the
  slash as an ID separator and 404s every sub-resource.

### Mattermost idiosyncrasies
- API endpoints need **26-char channel IDs**, not names — 404s otherwise. Added
  `_resolve_channel()` with a process-wide cache (`aa79b44`).
- **PATs require `MM_SERVICESETTINGS_ENABLEUSERAPITOKEN=true`** on the server.
  Without it the seed silently falls back to a session token; demo works once,
  then 401s mysteriously the next day.
- Empty bearer token → httpx crashes with "Illegal header value" before any
  HTTP call. Required an early `ToolException(UNAUTHORIZED)` with a pointer to
  the right env var (`d8d3a2e`).

### Config / env
- **Three independent code paths** were reading env vars with different names
  (`MM_ADMIN_*` vs `TLA_MATTERMOST_*`, bash-only `GITLAB_ADMIN_TOKEN`,
  pydantic-settings). Consolidated to a single `.env` + `TLA_` prefix
  (`fa62456`).
- **Process env beats `.env` file** in pydantic-settings. A stale
  `export TLA_GITLAB_TOKEN` in another terminal silently defeats the seed's
  self-healing PAT provisioning. Worth a startup-time assertion.
- **Bash operator precedence**: `$(git rev-parse --show-toplevel || cd ... && pwd)`
  ran *both* branches when the first succeeded, concatenating paths with a
  newline (`ee0e56c`).
- `python3` on the host ≠ project venv. `seed.sh` had to switch to
  `uv run python` to get `httpx`/`pyyaml` (`2f2acf0`).

### Agent behavior
- The canonical "confidently lied" case lives in
  `evals/cases/14_premature_stop_should_dig_more.yaml`. Agent claimed to have
  checked ticket dependencies but never called `get_ticket_dependencies`.
  `src/tl_agent/agent/verifier.py::check_success_claim` cross-checks
  success-claims against the recent tool transcript.
- **Local/smaller models are noticeably more verbose**. Had to bump
  `ResponseDraft.rationale` max_length from 400 → 1500 to accommodate Ollama
  runs (`d97a428`).
- Phase 6 can fail to produce a clean draft. Added `needs_review` flag so the
  UI flags it for human review instead of failing the run.
- **Agent column-confusion** (e.g. `sha=gitlab_admin_3841f1`) — model pulls a
  sha from the author-email prefix. Verifier catches "claimed X but never
  called Y" but not "called Y with garbage." Belongs under
  `TOOL_ARG_HALLUCINATION` in the eval taxonomy.

### Infra choices
- **Mock the painful one, run the easy one.** Jira's docker image is ~6 GB and
  10–15 min first-boot. GitLab CE boots in ~90s. So Jira is FastAPI mock,
  GitLab is real.
- **Failure taxonomy with per-bucket acceptance rates** beats a single
  pass-rate number. `IDEMPOTENCY_VIOLATION` and `READBACK_FAILURE` at 1.00
  (zero tolerance); `TOOL_ARG_HALLUCINATION` at 0.95.

---

## Build-time / static-analysis

### Python 3.12 typing
- `Field(default_factory=list)` makes pyright unhappy — it can't infer the
  generic. Fix: `default_factory=list[Evidence]` (subscripted-generic
  callable). Applied across `triage.py`, `hotspot.py`, `signals.py`.
- ruff `UP046` flags `Generic[T]` in 3.12. Switched to PEP 695:
  `class ToolResult[OutputT: BaseModel]:` and `async def readback[T](...)`.
- `@contextmanager` should return `Generator`, not `Iterator`, for pyright
  strict.
- FastAPI `@app.on_event("startup")` is deprecated — use `lifespan` with
  `@asynccontextmanager` returning `AsyncGenerator[None]` (not `AsyncIterator`).
- `NoReturn` on raise-helpers (`raise_from_http_error`) is required for mypy
  to know the call diverges.
- `hasattr(result, "value")` ≠ type narrowing. Use `isinstance(result, ToolResult)`
  so mypy/pyright propagate the type.
- mypy `--strict` flags every now-stale `# type: ignore` after a root fix —
  they must be deleted, not left "just in case."

### ruff strict surprises
- `S105` ("possible hardcoded password") fires on enum string values like
  `TOKEN_BUDGET = "token_budget"` and `PASS = "pass"`. Per-line `# noqa: S105`
  is the pragmatic fix.
- `B017` rejects `pytest.raises(Exception)`. Use specific types:
  `pytest.raises((AttributeError, TypeError))` or `ValidationError`.
- `PERF401` rewrites every `for x in ys: out.append(...)` into
  `out.extend(... for x in ys)`. Aggressive but consistent.
- `del unused_var` is the cleanest way to silence `ARG002` for
  interface-parity params (e.g. `cache_system` on the Ollama provider).

### Third-party type holes
- `typer.Option` / `typer.Argument` are overload-based with `Any` returns —
  drives pyright nuts. Downgrade `reportUnknownVariableType` to warning
  rather than fight it.
- `slack-sdk` is largely untyped — generates ~29 third-party warnings;
  tolerated, not fixed. `open_resp["channel"]["id"]` needs `.get(...) or {}`
  pattern to avoid index errors on `Any`.
- `slack-sdk`'s `AsyncWebClient` silently requires `aiohttp` — not listed in
  their deps. Had to add it explicitly to `pyproject.toml`.
- `SpanProcessor` is exported from `opentelemetry.sdk.trace`, not
  `.trace.export` where you'd expect.
- The MCP SDK is an optional extra — easier to exclude `mcp/` from strict
  type-checking than to fight conditional imports.

### SQLite + asyncio
- `sqlite3.Connection` can't be shared across threads —
  `asyncio.to_thread` on every read breaks tests. Dropped it: SQLite ops on
  local files are sub-ms and don't justify cross-thread complexity. The
  orchestrator owns one connection per run.

### OTel quirks
- `trace.set_tracer_provider` is guarded by `_TRACER_PROVIDER_SET_ONCE`. Tests
  need to reset both `_TRACER_PROVIDER_SET_ONCE` and `_TRACER_PROVIDER` to
  install a fresh provider per test.
- `ReadableSpan.attributes` returns a generic mapping — type it explicitly as
  `dict[str, object]` in serialization code.

### Test patterns that bit
- `ScriptedProvider(script)` copying the list at construction means
  post-construction mutation has no effect. Build the varied script first,
  then construct.
- Hardcoded epoch-millis timestamps in `httpx_mock` assertions drift relative
  to `datetime` — derive both from the same `datetime` instance.

### Markdown parsing gotcha
- The team config used `- **id:** john` (colon inside bold) but the initial
  regex expected `- **id**: john` (colon outside). Accept both:
  `r"^-\s+\*\*(?P<key>[^*]+?)\*\*:?\s*(?P<value>.+?)\s*$"` plus `rstrip(":")`
  on the key.

---

## Architectural decisions captured during the build

Defended in commit messages — worth preserving for future sessions.

1. **Stop reasons as a named enum** (`StopReason.MAX_STEPS` etc.) so "why did
   the loop stop" is queryable from traces, not buried in log strings.
2. **Success-claim detector runs deterministically before the LLM verifier**
   — cheap structural check first, expensive LLM audit only on mismatch.
3. **Writer tools excluded at registry bind time**, not just at call time.
   Defense in depth — Phase 5 literally cannot see writers in its tool list.
4. **Idempotency key scoped to `run_date_iso`** — same call on a new day
   deliberately re-runs. Cross-day dedup would be a different design.
5. **JSONL sink alongside Phoenix** — grep-able, git-diffable,
   CI-artifact-able. Phoenix is for browsing live; JSONL is for everything
   else.
6. **`make check` as the single gate**, pre-commit hook enforces. ruff +
   mypy --strict + pyright --strict + unit tests. No skipping.
7. **Prompts never edited in place** — every change is `vN+1.md` with
   `parent_version` + `change_reason` in frontmatter.
8. **Chat provider chosen by config alone** — `TLA_CHAT_PROVIDER=mattermost|slack`,
   no code path differences at call sites.

---

## Design-phase findings (agentic architecture)

These came out of the design conversation *before* code, not from commit
archaeology. They shaped the 8-phase shape itself.

### The 2026 pattern: deterministic skeleton, agentic nodes
The "bag of MCP tools + one ReACT agent" approach (popular in 2024) lost
favor for concrete reasons:
- **Tool overload** degrades selection past ~20–30 tools.
- **Shared context window** gets polluted by raw Jira/GitLab payloads.
- **Non-deterministic control flow** is nearly impossible to debug ("why did
  it skip Alex's stuck PR?").
- **Latency stacks** as the agent re-decides things it should just do.

What replaced it: a hand-coded pipeline where each stage that needs
reasoning is its own LLM call with its own tool subset and its own context.
**The flow is code; the intelligence is local.** That's why this repo has
8 phases, not one ReACT loop over everything.

### Context isolation is the whole game
- Per-engineer enrichers (Phase 2) see only that engineer's slice.
- The cross-correlator (Phase 3) never sees raw Jira JSON — only distilled
  per-engineer triages.
- The deep-dive ReACT loop (Phase 5) gets one hot spot at a time, not the
  whole brief.

This is what lets each call actually reason instead of drowning in payload.

### ReACT belongs *inside* a node, not at the top
The top-level flow is deterministic (always collect, always triage, always
correlate). The "should I dig deeper into this PR?" decision is the right
place for a tool-use loop — that's a genuine choice with no obvious answer.
Phase 5 is the only ReACT loop in the pipeline.

### Actions separated from observations
Reading is autonomous; writing (Slack pings, ticket comments, reassignments)
goes through a human approval gate. Phase 8 is deliberately not chained to
Phase 7 — the orchestrator stops at "drafts ready," and the TL drives
execution from the web UI. Becoming non-negotiable for production agentic
systems in 2026.

### Memory ≠ context, and one store is wrong
Three distinct access patterns need three stores:
- **Structured state** (flags, predictions, days-hot counters) → SQLite.
- **Free-text history** ("anything said about X?") → FTS5 (vector store later
  if recall demands it).
- **Persistent facts** (team, ownership, TL preferences) → flat markdown
  files, hand-editable, version-controlled.

Don't memorize state derivable from live APIs (current ticket status —
fetch it). Memorize the *interpretation* (your flag, your prediction).

### Auditability beats magic memory
Avoided Mem0/Letta/Zep-style "LLM decides what to remember" — they demo
well but make memory misses painful to debug. We want the TL to ask "why
did you flag Alex?" and see the exact memory used. LLM-driven extraction
hides that.

### The 2026 honest label is "hybrid"
The taxonomy worth getting right:
- **Workflow / pipeline**: code decides what happens next.
- **Agent**: model decides what happens next.
- **Hybrid**: workflow with one or more agentic nodes inside.

This system is a hybrid — "workflow with one agentic triage stage." More
agentic ≠ better; that's the lesson 2025 hammered home. Pure ReACT agents
over many tools are harder to debug, more expensive, less reliable.

### Workflow-design surprises (revising the user's first draft)
The initial workflow draft was a *uniform-depth sweep* — equal attention
to every engineer and every ticket. Real TLs do **fast triage, pick 2–3
hot spots, spend time there**. Specific gaps the revision added:

1. **Prioritization pass** before deep-dive — without it, 20 minutes spent
   equally where 5 + 15 on the right thing was the right shape.
2. **Yesterday's flags loop closure FIRST** (Phase 0) — first thing a real
   TL does mentally is "what did I flag yesterday, did it resolve?" The
   original draft re-discovered concerns daily.
3. **Cross-engineer correlation** (Phase 3) — most useful signals are
   patterns (two blocked on the same thing, three tickets in one module).
   Sequential per-person processing misses all of them.
4. **Response-mode decision** (Phase 6) — without picking explicitly between
   NOTE / STANDUP-mention / DM / ESCALATE, the agent defaults to "ping the
   engineer" for everything and becomes a nag.
5. **Engineer baselines** (rolling 2-week window) — "Maria's standup is
   short" is signal only relative to her baseline, not in absolute terms.
6. **TL's own state is the biggest signal** — yesterday's flags, questions
   she sent, predictions she made. Easy to miss when modeling "the team."

### Decision points where the LLM actually earns its keep
Not "every step." Specifically:
- Per-engineer triage (green/yellow/red + why)
- Correlation across signals into coherent narratives
- Response-mode selection
- Composing messages that are specific and not annoying

Everything else (collection, counter math, reconciliation, idempotent
execution) is deterministic code. Letting the model into those costs
latency and reliability for no gain.

### Production-runtime gaps the architecture sketch missed
Listed candidly during design — what separates "control flow looks right"
from "this could actually run in prod":
- **Verification / loop closure** — the act → observe → compare → repair
  cycle. Hallucinating success is the #1 reason demos work and prod
  doesn't. Drove `verifier.py` and the `check_success_claim` deterministic
  guard.
- **Sandboxed execution** — moot here because no tool runs user-supplied
  code, but the registry's `requires_approval` gate is the seam.
- **State persistence & resumability** — runs must snapshot per phase so
  they replay deterministically. JSONL trace + SQLite state cover this.
- **Tool orchestration beyond MCP** — selection (filter to relevant
  subset), parallel composition, per-tool retry policies, secret scoping.
  MCP is a protocol; orchestration is the layer above.
- **Side-effect ledger with idempotency keys** — every external action
  logged with `(tool, args, run_date)` hash so retries can't double-post.
- **Cost & runaway control** — per-run token/time budgets, hard stops on
  loops. One bad prompt without these eats a month.
- **Real-time interruption** — pause/resume as a first-class harness
  primitive, not kill-and-restart. Not built yet; flagged.

### What "closes the loop" actually means
This phrase does a lot of work in production-agent job descriptions. It is
not "does the right thing." It is:

```
act → observe state → compare to intent →
   if diverges: repair / retry / escalate
```

For coding agents: write → run tests → read failures → fix → re-run.
For action agents: call API → query back → diff observed vs. intended →
done or repair. **The agent never trusts its own optimism.**

The verification step has to be mandatory and built into the runtime, not
a prompt instruction. "Please verify your work" is wishful thinking; a
harness that forces a verifier call after every state-changing action is
engineering. Phase 8's readback (`readback.py`) is the concrete instance.

### Build vs buy the harness
Considered LangChain / LangGraph / Mastra / Pydantic AI / OpenAI Assistants.
Conclusion: **build your own thin harness** — ~300–500 lines (we ended up
around 250 in `agent/loop.py`). Frameworks give breadth, cost fit. The
abstractions are now well-understood enough that rolling your own is the
serious-systems default in 2026. "We built it on LangChain" is not a
credible answer to "tell me about a time the agent confidently lied."

---

## Patterns that recur

1. **Most fixes were surfaced by end-to-end runs, not unit tests.** Commit
   subjects literally say "Surfaced by an end-to-end `tl-agent run`". Eval
   harness + traces are the senior-signal investment.
2. **Vendor APIs lie / version-drift fast.** Half the fixes are "the API
   changed and I learned by 4xx-ing." Prefix-matchers + caches paper over
   this.
3. **Config has too many sources of truth by default.** The .env consolidation
   isn't glamorous but every other invisible bug came from divergent reads.
4. **Strict static analysis catches its own surprises.** Most of the
   build-time list above is the price of `mypy --strict` + `pyright --strict`
   + ruff strict mode. The price is paid once; the value is permanent.

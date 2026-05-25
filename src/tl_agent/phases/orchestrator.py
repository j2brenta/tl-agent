"""Orchestrator — wires P0 → P7 with per-phase OTel spans.

P8 is deliberately NOT chained here: it waits for the TL's approval via
the web UI. The orchestrator returns the per-run Brief; the UI invokes
`phase8_execute.execute_decision()` for each TL action.

This is the single entry point: `await run(date)`. The CLI calls it; the
eval harness calls it; the MCP server's `run_evals` calls it.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date

from tl_agent.llm.budget import BudgetTracker
from tl_agent.obs.spans import phase_span
from tl_agent.obs.tracing import init_tracing
from tl_agent.phases import (
    phase0_loop_closure,
    phase1_collect,
    phase2_triage,
    phase3_correlate,
    phase4_reconcile,
    phase5_deepdive,
    phase6_response_mode,
    phase7_compose,
)
from tl_agent.phases._context import RunContext
from tl_agent.phases.phase7_compose import Brief
from tl_agent.settings import get_settings
from tl_agent.storage import connect, initialize, load_team
from tl_agent.storage.repos import flags as flags_repo
from tl_agent.tools import SqliteIdempotencyStore, registry
from tl_agent.tools.chat.tools import register_chat_tools
from tl_agent.tools.gitlab import register_gitlab_tools
from tl_agent.tools.jira import register_jira_tools
from tl_agent.tools.memory import register_memory_tools, set_sqlite_connection

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunResult:
    """End-of-run summary the CLI prints + the eval harness asserts against."""

    run_id: str
    run_date: date
    brief: Brief
    open_flag_count: int
    closed_flag_count: int
    deep_dives_count: int
    notes: list[str] = field(default_factory=list[str])


async def run(run_date: date | None = None) -> RunResult:
    """Run the full P0 → P7 pipeline. Returns a RunResult."""
    init_tracing()
    _register_all_tools()

    s = get_settings()
    run_date = run_date or date.today()
    run_id = f"run-{uuid.uuid4().hex[:12]}"

    conn = connect(s.sqlite_path)
    initialize(conn)
    set_sqlite_connection(lambda: conn)

    # Build the router lazily — needs anthropic_api_key but doesn't actually
    # call out until a phase uses it. Tolerant of no API key during scaffold tests.
    from tl_agent.llm.router import build_default

    router = build_default()
    idempotency = SqliteIdempotencyStore(conn)
    budget = BudgetTracker(token_cap=s.run_token_budget)
    team = load_team()

    ctx = RunContext(
        run_id=run_id,
        run_date=run_date,
        sqlite=conn,
        router=router,
        team=team,
        idempotency=idempotency,
        budget=budget,
    )

    return await _run_pipeline(ctx)


@phase_span("orchestrator.run")
async def _run_pipeline(ctx: RunContext) -> RunResult:
    """The actual phase chain. Separate from `run()` so tests can inject ctx."""
    # Phase boundaries are logged at INFO so `tl-agent run` (verbose by
    # default) shows progress — the difference between "hung" and "Phase 5
    # is running a long ReACT loop on a local model".
    import time as _time

    def _started(name: str) -> float:
        logger.info("→ %s", name)
        return _time.perf_counter()

    def _done(name: str, t0: float) -> None:
        logger.info("✓ %s (%.1fs)", name, _time.perf_counter() - t0)

    t = _started("phase0_loop_closure")
    await phase0_loop_closure.run(ctx)
    _done("phase0_loop_closure", t)

    t = _started("phase1_collect")
    signals = await phase1_collect.run(ctx)
    _done("phase1_collect", t)

    t = _started("phase2_triage")
    per_engineer = await phase2_triage.run(ctx, signals)
    _done("phase2_triage", t)

    t = _started("phase3_correlate")
    hotspots = await phase3_correlate.run(ctx, signals=signals, per_engineer=per_engineer)
    _done("phase3_correlate", t)

    t = _started("phase4_reconcile")
    reconciled = await phase4_reconcile.run(ctx, today_hotspots=hotspots)
    _done("phase4_reconcile", t)

    t = _started("phase5_deepdive")
    deep_dives = await phase5_deepdive.run(ctx, hotspots=reconciled.hotspots)
    _done("phase5_deepdive", t)

    t = _started("phase6_response_mode")
    drafts = await phase6_response_mode.run(ctx, deep_dives=deep_dives)
    _done("phase6_response_mode", t)

    t = _started("phase7_compose")
    brief = await phase7_compose.run(ctx, drafts=drafts, deep_dives=deep_dives)
    _done("phase7_compose", t)

    open_flags = len(flags_repo.list_open_on(ctx.sqlite, ctx.run_date))
    return RunResult(
        run_id=ctx.run_id,
        run_date=ctx.run_date,
        brief=brief,
        open_flag_count=open_flags,
        closed_flag_count=len(reconciled.closed_flag_ids),
        deep_dives_count=len(deep_dives),
        notes=list(ctx.notes),
    )


def _register_all_tools() -> None:
    """Ensure every tool module has registered into the central registry."""
    registry.reset()  # idempotent — keeps test isolation clean too
    register_jira_tools()
    register_gitlab_tools()
    register_memory_tools()
    register_chat_tools()

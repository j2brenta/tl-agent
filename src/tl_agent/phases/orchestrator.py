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
from typing import Any

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
    sprint_select,
)
from tl_agent.phases._context import RunContext
from tl_agent.phases.phase7_compose import Brief
from tl_agent.settings import get_settings
from tl_agent.storage import connect, initialize, load_team, transaction
from tl_agent.storage.repos import flags as flags_repo
from tl_agent.storage.repos import observations as obs_repo
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
    commits_count: int = 0
    standups_count: int = 0
    notes: list[str] = field(default_factory=list[str])


async def run(run_date: date | None = None, sprint_id: str | None = None) -> RunResult:
    """Run the full P0 → P7 pipeline. Returns a RunResult.

    `sprint_id`, when given, is a human-resolved sprint from the Workflow tab:
    it skips discovery and operates over that sprint directly.
    """
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
        sprint_id=sprint_id,
    )

    return await _run_pipeline(ctx)


@phase_span("orchestrator.run")
async def _run_pipeline(ctx: RunContext) -> RunResult:
    """The actual phase chain. Separate from `run()` so tests can inject ctx."""
    import json
    import time as _time
    from datetime import UTC, datetime

    phase_log: list[dict[str, object]] = []

    def _started(name: str) -> float:
        logger.info("→ %s", name)
        return _time.perf_counter()

    def _done(name: str, t0: float, status: str = "ok") -> None:
        dur = round(_time.perf_counter() - t0, 2)
        logger.info("✓ %s (%.1fs)", name, dur)
        phase_log.append({"phase": name, "status": status, "duration_s": dur})

    signals_summary: dict[str, Any] = {}
    sprint_decision: dict[str, Any] = {}

    def _save_run(status: str) -> None:
        notes_payload = json.dumps(
            {
                "phases": phase_log,
                "errors": list(ctx.notes),
                "signals": signals_summary,
                "sprint_decision": sprint_decision,
            }
        )
        ctx.sqlite.execute(
            """
            INSERT INTO runs (id, run_date, started_at, status, trace_id, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                finished_at = excluded.finished_at,
                status      = excluded.status,
                notes       = excluded.notes
            """,
            (
                ctx.run_id,
                ctx.run_date.isoformat(),
                datetime.now(UTC).isoformat(),
                status,
                getattr(ctx, "trace_id", None),
                notes_payload,
            ),
        )

    _save_run("in_progress")

    # Pre-flight: pick the sprint to operate over. A human-resolved sprint_id
    # skips discovery; otherwise we discover and may park the run for a human.
    t = _started("sprint_select")
    if ctx.sprint_id is not None:
        sprint_decision.update(
            {
                "state": "resolved",
                "chosen": ctx.sprint_id,
                "candidates": [],
                "reason": "human-selected",
            }
        )
        _done("sprint_select", t)
    else:
        selection = await sprint_select.run(ctx)
        sprint_decision.update(
            {
                "state": selection.state,
                "chosen": selection.chosen_sprint_id,
                "candidates": selection.candidates,
                "reason": selection.reason,
            }
        )
        if selection.state == "pending":
            _done("sprint_select", t, status="awaiting")
            _save_run("awaiting_sprint")
            logger.info(
                "run.awaiting_sprint", extra={"run_id": ctx.run_id, "reason": selection.reason}
            )
            return RunResult(
                run_id=ctx.run_id,
                run_date=ctx.run_date,
                brief=Brief(decisions=[]),
                open_flag_count=0,
                closed_flag_count=0,
                deep_dives_count=0,
                notes=[*ctx.notes, f"awaiting sprint decision: {selection.reason}"],
            )
        ctx.sprint_id = selection.chosen_sprint_id
        _done("sprint_select", t)

    t = _started("phase0_loop_closure")
    await phase0_loop_closure.run(ctx)
    _done("phase0_loop_closure", t)

    t = _started("phase1_collect")
    signals = await phase1_collect.run(ctx)
    _done("phase1_collect", t)
    signals_summary.update(
        {
            "commits": len(signals.commits),
            "commit_dates": sorted({c.committed_at.strftime("%Y-%m-%d") for c in signals.commits}),
            "standups_today": len(signals.standups_today),
            "sprint_tickets": len(signals.sprint_tickets),
            "tickets_added_since_yesterday": len(signals.tickets_added_since_yesterday),
        }
    )

    # Persist today's standup observations so the sprint UI can display them
    # without requiring the full 8-phase pipeline to complete first.
    with transaction(ctx.sqlite):
        for msg in signals.standups_today:
            obs_repo.upsert(
                ctx.sqlite,
                obs_id=f"{ctx.run_date.isoformat()}:{msg.engineer_id}",
                run_date=ctx.run_date,
                engineer_id=msg.engineer_id,
                raw=msg.raw,
                summary=None,
                chat_message_id=msg.chat_message_id,
            )

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

    _save_run("completed")

    open_flags = len(flags_repo.list_open_on(ctx.sqlite, ctx.run_date))
    return RunResult(
        run_id=ctx.run_id,
        run_date=ctx.run_date,
        brief=brief,
        open_flag_count=open_flags,
        closed_flag_count=len(reconciled.closed_flag_ids),
        deep_dives_count=len(deep_dives),
        commits_count=signals_summary.get("commits", 0),
        standups_count=signals_summary.get("standups_today", 0),
        notes=list(ctx.notes),
    )


def _register_all_tools() -> None:
    """Ensure every tool module has registered into the central registry."""
    registry.reset()  # idempotent — keeps test isolation clean too
    register_jira_tools()
    register_gitlab_tools()
    register_memory_tools()
    register_chat_tools()

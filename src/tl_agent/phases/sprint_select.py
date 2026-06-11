"""Pre-flight — discover the team's current sprint.

Runs before Phase 1. Lists the sprints on the team's board, keeps the ones
whose name matches the configured pattern and aren't completed, and looks for
the single `active` one. When exactly one active in-scope sprint exists we
auto-select it; otherwise (zero, or several) we hand the decision to a human
via the Workflow tab rather than guess.

The orchestrator records the outcome in the run's `notes.sprint_decision`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from tl_agent.models import JiraSprint, JiraSprintState
from tl_agent.obs.spans import phase_span
from tl_agent.phases._context import RunContext
from tl_agent.phases._sprint import sprint_progress
from tl_agent.tools import ToolResult
from tl_agent.tools.jira import ListSprintsTool

logger = logging.getLogger(__name__)

SelectionState = Literal["auto", "pending"]


@dataclass(frozen=True)
class SprintSelection:
    """Outcome of sprint discovery.

    `auto` → `chosen_sprint_id` is set (or None to mean "fall back to active");
    the pipeline proceeds. `pending` → ambiguity that needs a human; the
    orchestrator parks the run as `awaiting_sprint` and surfaces `candidates`.
    """

    state: SelectionState
    reason: str
    chosen_sprint_id: str | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])


def _candidate_dict(s: JiraSprint, run_date: date) -> dict[str, Any]:
    day, length = sprint_progress(s.start_date, s.end_date, run_date)
    return {
        "id": s.id,
        "name": s.name,
        "state": s.state.value,
        "sprint_day": day if s.state is JiraSprintState.ACTIVE else None,
        "sprint_length_days": length,
    }


@phase_span("sprint_select")
async def run(ctx: RunContext) -> SprintSelection:
    """Discover the team's current sprint.

    Pull the board's **active** sprints, keep those whose name matches the team
    pattern, and auto-select when exactly one survives. Zero or several matches
    propagate the "which sprint is current?" decision to a human (the run parks
    as `awaiting_sprint` and the candidates surface on the Workflow tab).
    """
    board_id = ctx.team.board_id
    pattern = ctx.team.sprint_name_pattern
    if not board_id or not pattern:
        # No board/pattern configured — without a board there's no sprint to
        # discover, so Phase 1 will collect an empty sprint and note it.
        ctx.notes.append("sprint_select: board_id/sprint_name_pattern not configured; using active")
        return SprintSelection(state="auto", reason="no board/pattern configured")

    tool = ListSprintsTool()
    outcome = await tool.invoke(
        {"board_id": board_id, "state": "active"}, run_date_iso=ctx.run_date_iso
    )
    if not isinstance(outcome, ToolResult):
        ctx.notes.append(
            f"sprint_select: active sprint list failed ({outcome.kind.value}); using active"
        )
        return SprintSelection(state="auto", reason=f"discovery failed: {outcome.kind.value}")

    # Defensive: keep only active sprints even if the API ignored `state`.
    active = [s for s in outcome.value.sprints if s.state is JiraSprintState.ACTIVE]
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        ctx.notes.append(f"sprint_select: bad sprint_name_pattern {pattern!r}: {exc}; using active")
        return SprintSelection(state="auto", reason=f"bad pattern: {exc}")

    team_active = [s for s in active if rx.search(s.name)]

    logger.info(
        "sprint_select.discovered",
        extra={
            "run_date": ctx.run_date_iso,
            "board_id": board_id,
            "pattern": pattern,
            "active_total": len(active),
            "team_active": [s.id for s in team_active],
        },
    )

    if len(team_active) == 1:
        chosen = team_active[0]
        return SprintSelection(
            state="auto",
            reason=f"single active in-scope sprint: {chosen.name}",
            chosen_sprint_id=chosen.id,
            candidates=[_candidate_dict(chosen, ctx.run_date)],
        )

    # Zero or several active team sprints — a human decides which is current.
    # Offer the team-scoped active set when non-empty, else every active sprint
    # on the board so they can still pick one.
    picker = team_active or active
    reason = (
        "no active sprint matches the team scope"
        if not team_active
        else f"{len(team_active)} active sprints match the team scope"
    )
    ctx.notes.append(f"sprint_select: {reason}; awaiting human decision")
    return SprintSelection(
        state="pending",
        reason=reason,
        candidates=[_candidate_dict(s, ctx.run_date) for s in picker],
    )

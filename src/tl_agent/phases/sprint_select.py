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
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from tl_agent.models import JiraSprint, JiraSprintState
from tl_agent.obs.spans import phase_span
from tl_agent.phases._context import RunContext
from tl_agent.phases._sprint import sprint_progress
from tl_agent.storage.repos import resolved_config
from tl_agent.tools import ToolResult
from tl_agent.tools.jira import JiraBoard, ListBoardsTool, ListSprintsTool

logger = logging.getLogger(__name__)

SelectionState = Literal["auto", "pending"]
# Which decision a `pending` selection is asking the human to make.
DecisionKind = Literal["sprint", "board"]


@dataclass(frozen=True)
class SprintSelection:
    """Outcome of sprint discovery.

    `auto` → `chosen_sprint_id` is set (or None to mean "fall back to active");
    the pipeline proceeds. `pending` → ambiguity that needs a human; the
    orchestrator parks the run as `awaiting_sprint` and surfaces `candidates`.
    `kind` says what the human is picking — a sprint (the usual case) or, when
    `board_id` wasn't configured and discovery found several, a board.
    """

    state: SelectionState
    reason: str
    chosen_sprint_id: str | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    kind: DecisionKind = "sprint"


def _candidate_dict(s: JiraSprint, run_date: date) -> dict[str, Any]:
    day, length = sprint_progress(s.start_date, s.end_date, run_date)
    return {
        "id": s.id,
        "name": s.name,
        "state": s.state.value,
        "sprint_day": day if s.state is JiraSprintState.ACTIVE else None,
        "sprint_length_days": length,
    }


async def _discover_boards(run_date_iso: str) -> list[JiraBoard] | str:
    """Live board discovery. Returns the boards, or the tool failure-kind string."""
    outcome = await ListBoardsTool().invoke({}, run_date_iso=run_date_iso)
    if not isinstance(outcome, ToolResult):
        return outcome.kind.value
    return list(outcome.value.boards)


def _cache_board(sqlite: sqlite3.Connection, board: JiraBoard) -> str:
    """Persist a discovered board as the learned default; return its id."""
    resolved_config.set(sqlite, resolved_config.JIRA_BOARD_KEY, board.id)
    return board.id


async def resolve_board_id(
    sqlite: sqlite3.Connection,
    *,
    board_id_override: str | None,
    run_date_iso: str,
    notes: list[str] | None = None,
) -> str | None:
    """Best-effort board resolution to a single id, with no human gate.

    Precedence: config override → DB resolved cache → live discovery (a single
    discovered board is cached). Returns None when nothing settles on one board
    (discovery failed, found zero, or found several). Callers without a
    human-decision path — Phase 1's collect, the standalone "Collect" button —
    use this; `_resolve_board` wraps the same primitives with the multi-board
    gate for the orchestrator pre-flight.
    """
    board_id = board_id_override or resolved_config.get(sqlite, resolved_config.JIRA_BOARD_KEY)
    if board_id:
        return board_id

    discovered = await _discover_boards(run_date_iso)
    if isinstance(discovered, str):
        if notes is not None:
            notes.append(f"board discovery failed ({discovered})")
        return None
    if len(discovered) == 1:
        chosen = discovered[0]
        if notes is not None:
            notes.append(f"discovered Jira board {chosen.id!r} ({chosen.name}); cached")
        return _cache_board(sqlite, chosen)
    if notes is not None:
        notes.append(
            "no Jira boards discovered"
            if not discovered
            else f"{len(discovered)} Jira boards — set board_id in config/team.md to pin one"
        )
    return None


async def _resolve_board(ctx: RunContext) -> str | SprintSelection:
    """Resolve the team's Jira board, returning its id or a decision to bubble up.

    Precedence: `config/team.md` override → DB resolved cache → live discovery.
    Exactly one discovered board is cached and used; several hand a "which
    board?" choice to a human; none degrades to no-sprint.
    """
    board_id = ctx.team.board_id or resolved_config.get(ctx.sqlite, resolved_config.JIRA_BOARD_KEY)
    if board_id:
        return board_id

    discovered = await _discover_boards(ctx.run_date_iso)
    if isinstance(discovered, str):
        ctx.notes.append(f"sprint_select: board discovery failed ({discovered})")
        return SprintSelection(state="auto", reason=f"board discovery failed: {discovered}")

    boards = discovered
    if len(boards) == 1:
        chosen = boards[0]
        _cache_board(ctx.sqlite, chosen)
        ctx.notes.append(
            f"sprint_select: discovered Jira board {chosen.id!r} ({chosen.name}); cached. "
            f"Add `- **board_id:** {chosen.id}` under Sprint scope in config/team.md to pin it."
        )
        return chosen.id
    if not boards:
        ctx.notes.append("sprint_select: no Jira boards discovered")
        return SprintSelection(state="auto", reason="no Jira boards discovered")

    ctx.notes.append(f"sprint_select: {len(boards)} Jira boards; awaiting human board choice")
    return SprintSelection(
        state="pending",
        kind="board",
        reason="multiple Jira boards — choose the team's board",
        candidates=[{"id": b.id, "name": b.name} for b in boards],
    )


@phase_span("sprint_select")
async def run(ctx: RunContext) -> SprintSelection:
    """Discover the team's current sprint.

    First resolve the board (config override → DB cache → discovery, asking a
    human if several boards exist). Then pull that board's **active** sprints,
    keep those whose name matches the team pattern, and auto-select when exactly
    one survives. Zero or several matches propagate the "which sprint is
    current?" decision to a human (the run parks as `awaiting_sprint` and the
    candidates surface on the Workflow tab).
    """
    board = await _resolve_board(ctx)
    if isinstance(board, SprintSelection):
        return board
    board_id = board

    pattern = ctx.team.sprint_name_pattern
    if not pattern:
        # Board known but no team filter — can't tell sprints apart; let Phase 1
        # collect an empty sprint rather than guess.
        ctx.notes.append("sprint_select: sprint_name_pattern not configured; using active")
        return SprintSelection(state="auto", reason="no sprint_name_pattern configured")

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

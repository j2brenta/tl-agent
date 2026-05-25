"""Daily flag — what the agent is watching, carried day-over-day."""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from tl_agent.models.triage import TriageStatus


class FlagType(StrEnum):
    """The category of an open flag."""

    INDIVIDUAL = "individual"  # one engineer hedging / stuck
    SHARED_BLOCKER = "shared_blocker"  # 2+ engineers, same dependency
    DEPENDENCY_HOTSPOT = "dependency_hotspot"  # 1 ticket blocks N
    OFF_SPRINT = "off_sprint"  # commits not tied to sprint
    SCOPE_CREEP = "scope_creep"  # estimate or diff size moving
    TEAM_VAGUENESS = "team_vagueness"  # team-wide signal


class Flag(BaseModel):
    """One thing the agent is tracking across days.

    `days_hot` increments when the same flag survives a reconcile (Phase 4);
    it resets when the flag is closed. >= 5 = escalation candidate (Phase 6).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    type: FlagType
    title: str = Field(min_length=1, max_length=400)
    engineer_id: str | None = None  # None for team-wide
    related_ticket_ids: tuple[str, ...] = ()
    status: TriageStatus
    days_hot: int = Field(ge=1, default=1)
    first_seen: date
    last_seen: date
    resolved: bool = False
    resolution_note: str | None = None

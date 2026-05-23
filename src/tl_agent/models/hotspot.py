"""Hot spot — a candidate that earns a Phase 5 deep-dive."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from tl_agent.models.flag import FlagType
from tl_agent.models.triage import Evidence, TriageStatus


class Hotspot(BaseModel):
    """One thing worth digging into today.

    Produced by Phase 3 (cross-correlate) and Phase 4 (reconcile). Phase 5
    iterates over these and runs a ReACT loop per item; Phase 6 picks a
    response mode for each.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    type: FlagType
    summary: str = Field(min_length=1, max_length=200)
    severity: TriageStatus
    days_hot: int = Field(ge=1, default=1)
    engineer_ids: tuple[str, ...] = ()  # empty for team-wide
    related_ticket_ids: tuple[str, ...] = ()
    evidence: list[Evidence] = Field(default_factory=list[Evidence], max_length=12)
    sprint_critical: bool = False

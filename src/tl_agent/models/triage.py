"""Per-engineer triage status and supporting evidence.

Produced by Phase 2 (triage); consumed by Phase 3 (correlate) and Phase 5
(deep-dive). Every triage call must return structured output that conforms to
`EngineerTriage` — the LLM is constrained via Pydantic JSON schema.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class TriageStatus(StrEnum):
    """Coarse per-engineer status for the day."""

    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


class EvidenceKind(StrEnum):
    """The provenance of one evidence pointer."""

    TICKET = "ticket"
    COMMIT = "commit"
    STANDUP = "standup"
    BASELINE = "baseline"
    DEPENDENCY = "dependency"


class Evidence(BaseModel):
    """One pointer the LLM cited when justifying a status.

    Keep these tight: an evidence row should fit on one line in the brief.
    `ref` is whatever identifier makes sense for the kind (ticket key, commit
    SHA, ISO date, etc.) so downstream tools can fetch the full context.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EvidenceKind
    ref: str = Field(min_length=1)
    note: str = Field(min_length=1, max_length=240)


class EngineerTriage(BaseModel):
    """Phase 2 output for one engineer."""

    model_config = ConfigDict(extra="forbid")

    engineer_id: str = Field(min_length=1)
    status: TriageStatus
    one_line_reason: str = Field(min_length=1, max_length=160)
    evidence: list[Evidence] = Field(default_factory=list[Evidence], max_length=8)

    @property
    def is_attention_worthy(self) -> bool:
        """True for YELLOW or RED — gated for Phase 5 deep-dive."""
        return self.status in {TriageStatus.YELLOW, TriageStatus.RED}

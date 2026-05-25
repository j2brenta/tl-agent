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


class StandupExtract(BaseModel):
    """Structured read of free-prose standup text — what the engineer said.

    Filled by Phase 2 from the standup `raw` text, cross-checked against the
    engineer's sprint-ticket list. Lets the brief show "worked on X, also did
    PAY-99 (off-sprint), blocked on Y" without re-parsing the prose later.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    worked_on_tickets: list[str] = Field(
        default_factory=list[str],
        max_length=10,
        description="Sprint ticket keys (e.g. ENG-12) the engineer said they worked on",
    )
    off_sprint_work: list[str] = Field(
        default_factory=list[str],
        max_length=10,
        description="Other ticket keys or named work items mentioned that aren't in this sprint",
    )
    blockers: list[str] = Field(
        default_factory=list[str],
        max_length=10,
        description="Free-text blockers / dependencies / risks the engineer raised",
    )


class EngineerTriage(BaseModel):
    """Phase 2 output for one engineer."""

    model_config = ConfigDict(extra="forbid")

    engineer_id: str = Field(min_length=1)
    status: TriageStatus
    one_line_reason: str = Field(min_length=1, max_length=160)
    evidence: list[Evidence] = Field(default_factory=list[Evidence], max_length=8)
    extract: StandupExtract = Field(
        default_factory=StandupExtract,
        description="What the engineer said in prose — extracted and reconciled against the sprint",
    )

    @property
    def is_attention_worthy(self) -> bool:
        """True for YELLOW or RED — gated for Phase 5 deep-dive."""
        return self.status in {TriageStatus.YELLOW, TriageStatus.RED}

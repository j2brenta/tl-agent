"""Phase 6 / Phase 8 decisions — what response to take + the audit trail."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ResponseMode(StrEnum):
    """How the TL should respond to a hot spot.

    Selection heuristics (see plan, Phase 6):
      days_hot < 2                    → NOTE
      days_hot 2-4, individual        → DM
      days_hot 2-4, team-wide pattern → STANDUP
      days_hot >= 5 OR sprint-critical → ESCALATE
      off-sprint w/o justification    → DM
    """

    NOTE = "note"
    STANDUP = "standup"
    DM = "dm"
    ESCALATE = "escalate"


class ResponseDraft(BaseModel):
    """The pre-approval artifact composed in Phase 7.

    `body` is the actual text that would be sent (DM, standup question,
    escalation note). `target` is the recipient — engineer_id for DM,
    None for STANDUP, manager_id for ESCALATE, None for NOTE.
    """

    model_config = ConfigDict(extra="forbid")

    hotspot_id: str = Field(min_length=1)
    mode: ResponseMode
    target: str | None = None
    body: str = Field(min_length=1, max_length=2000)
    # 800 (was 400): smaller local models tend to be more verbose; the
    # rationale is for TL audit, not display, so the cap exists only to
    # bound storage, not to enforce concision.
    rationale: str = Field(min_length=1, max_length=1500)
    # True when Phase 6 couldn't get a clean draft and salvaged something
    # from the deep-dive instead. The web UI flags these for human review;
    # the LLM made its best attempt and the rest is the TL's call.
    needs_review: bool = Field(default=False)


class ApprovalAction(StrEnum):
    """What the TL did at Phase 8 review."""

    APPROVED = "approved"
    REJECTED = "rejected"
    EDITED = "edited"
    DEFERRED = "deferred"


class Decision(BaseModel):
    """One audit-log row.

    Recorded the moment a draft enters Phase 8 review and updated when the TL
    acts. `trace_id` lets the UI link out to Phoenix.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    created_at: datetime
    # ISO YYYY-MM-DD of the run that produced this decision; distinct from
    # `created_at` so date-filtering survives runs that straddle UTC midnight.
    run_date: str = Field(min_length=10, max_length=10)
    hotspot_id: str
    proposed_mode: ResponseMode
    proposed_body: str
    # Carried over from ResponseDraft.needs_review; renders a HITL banner in
    # the review UI so the TL knows this draft is salvage, not a clean LLM
    # output. Stored as 0/1 in SQLite via the decisions repo.
    needs_review: bool = False
    tl_action: ApprovalAction | None = None
    tl_acted_at: datetime | None = None
    final_body: str | None = None
    final_target: str | None = None
    trace_id: str | None = None
    sent_message_id: str | None = None
    sent_provider: str | None = None

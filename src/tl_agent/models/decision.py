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
    rationale: str = Field(min_length=1, max_length=400)


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
    hotspot_id: str
    proposed_mode: ResponseMode
    proposed_body: str
    tl_action: ApprovalAction | None = None
    tl_acted_at: datetime | None = None
    final_body: str | None = None
    final_target: str | None = None
    trace_id: str | None = None
    sent_message_id: str | None = None
    sent_provider: str | None = None

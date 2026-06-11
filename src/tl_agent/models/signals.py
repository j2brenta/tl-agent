"""Raw signal payloads from Jira / GitLab / chat.

These are the typed shapes that the integration tools return; Phase 1
collects them, Phase 2 reasons over them. We keep them narrow on purpose —
the agent never sees the full upstream JSON, only what's been validated here.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# -------------------- Jira --------------------


class JiraStatus(StrEnum):
    """Coarse Jira status bucket."""

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"
    BLOCKED = "blocked"


class JiraStatusChange(BaseModel):
    """One status transition from the ticket history."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    at: datetime
    by: str | None = None
    from_status: JiraStatus
    to_status: JiraStatus


class JiraEstimateChange(BaseModel):
    """One estimate change from the ticket history."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    at: datetime
    by: str | None = None
    from_points: float | None
    to_points: float | None


class JiraSprintState(StrEnum):
    """Lifecycle state of a sprint as Jira reports it."""

    ACTIVE = "active"
    CLOSED = "closed"
    FUTURE = "future"


class JiraSprint(BaseModel):
    """A sprint as the board lists it — metadata only, no tickets.

    `sprint_select` reasons over a list of these to decide which sprint the
    team is currently working over (the single `active` one whose name matches
    the team's configured pattern).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    state: JiraSprintState
    board_id: str | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None
    sprint_day: int | None = Field(default=None, ge=1)
    sprint_length_days: int | None = Field(default=None, ge=1)


class JiraTicket(BaseModel):
    """A sprint ticket as the agent sees it."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)
    summary: str
    status: JiraStatus
    assignee: str | None = None
    reporter: str | None = None
    points: float | None = None
    sprint: str | None = None
    created_at: datetime
    updated_at: datetime
    added_to_sprint_at: datetime | None = None
    blocks: tuple[str, ...] = ()
    blocked_by: tuple[str, ...] = ()
    status_history: tuple[JiraStatusChange, ...] = ()
    estimate_history: tuple[JiraEstimateChange, ...] = ()
    labels: tuple[str, ...] = ()


# -------------------- GitLab --------------------


class GitCommit(BaseModel):
    """One commit in the window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sha: str = Field(min_length=7)
    author: str
    committed_at: datetime
    branch: str | None = None
    message: str
    files_changed: int
    insertions: int
    deletions: int
    linked_ticket_keys: tuple[str, ...] = ()  # parsed from message


# -------------------- chat — see models.chat --------------------


class StandupMessage(BaseModel):
    """A standup post for a single engineer on a single day."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    engineer_id: str
    date_iso: str  # YYYY-MM-DD; standup is a daily artifact
    raw: str
    chat_message_id: str | None = None
    chat_channel_id: str | None = None


# -------------------- aggregate --------------------


class DailySignals(BaseModel):
    """Phase 1 aggregate.

    The single envelope passed from Phase 1 to Phase 2/3. Bounded size: the
    agent never sees raw upstream JSON, only what was validated into this.
    """

    model_config = ConfigDict(extra="forbid")

    run_date: str  # YYYY-MM-DD
    standups_today: list[StandupMessage] = Field(default_factory=list[StandupMessage])
    standups_yesterday: list[StandupMessage] = Field(default_factory=list[StandupMessage])
    sprint_tickets: list[JiraTicket] = Field(default_factory=list[JiraTicket])
    tickets_added_since_yesterday: list[JiraTicket] = Field(default_factory=list[JiraTicket])
    commits: list[GitCommit] = Field(default_factory=list[GitCommit])
    sprint_day: int = Field(ge=1)
    sprint_length_days: int = Field(ge=1)
    planned_points: float = 0.0
    completed_points: float = 0.0

"""Engineer identity and per-engineer metadata."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Role(StrEnum):
    """Structured team role.

    `role` (free-form, e.g. "senior backend") is the human-readable title;
    `role_kind` is the machine-readable role the workflow branches on —
    leadership (team lead, product manager) vs the engineers it operates over.
    """

    TEAM_LEAD = "team_lead"
    PRODUCT_MANAGER = "product_manager"
    ENGINEER = "engineer"


class Engineer(BaseModel):
    """One member of the team — engineer, team lead, or product manager.

    `id` is the stable identifier used across Jira/GitLab/chat — typically
    lowercase first name. Display name is for UI; aliases catch other handles
    the same person uses across systems.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    display_name: str
    role: str = Field(default="engineer")
    role_kind: Role = Role.ENGINEER
    jira_account_id: str | None = None
    gitlab_username: str | None = None
    chat_user_id: str | None = None
    email: str | None = None
    aliases: tuple[str, ...] = ()

    def matches(self, handle: str) -> bool:
        """True if `handle` refers to this engineer in any known system."""
        candidates = {
            self.id.lower(),
            self.display_name.lower(),
            (self.jira_account_id or "").lower(),
            (self.gitlab_username or "").lower(),
            (self.chat_user_id or "").lower(),
            (self.email or "").lower(),
            *(a.lower() for a in self.aliases),
        }
        candidates.discard("")
        return handle.lower() in candidates

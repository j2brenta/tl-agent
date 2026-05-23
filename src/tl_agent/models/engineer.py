"""Engineer identity and per-engineer metadata."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Engineer(BaseModel):
    """One member of the team.

    `id` is the stable identifier used across Jira/GitLab/chat — typically
    lowercase first name. Display name is for UI; aliases catch other handles
    the same person uses across systems.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    display_name: str
    role: str = Field(default="engineer")
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

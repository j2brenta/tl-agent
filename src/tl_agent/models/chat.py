"""Provider-agnostic chat models.

`ChatProvider` implementations (Mattermost primary, Slack stub) translate to/
from these shapes so the rest of the agent doesn't know which backend it's
talking to.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ChannelKind(StrEnum):
    """The shape of a channel — direct, group, or public-ish."""

    DM = "dm"
    GROUP = "group"
    CHANNEL = "channel"


class ChatUser(BaseModel):
    """A user as seen by the chat provider."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    username: str
    display_name: str | None = None
    email: str | None = None
    is_bot: bool = False


class ChatChannel(BaseModel):
    """A channel/DM target."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    name: str
    kind: ChannelKind


class ChatMessage(BaseModel):
    """A single message in a channel — input to Phase 1 standup parsing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    channel_id: str
    user_id: str
    created_at: datetime
    text: str
    thread_id: str | None = None
    edited_at: datetime | None = None


class PostResult(BaseModel):
    """What `post_dm` / `post_channel` return after readback succeeds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    message_id: str
    channel_id: str
    posted_at: datetime
    body: str

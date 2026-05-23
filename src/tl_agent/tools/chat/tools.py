"""Provider-agnostic chat tools — delegate to the configured ChatProvider.

These are what the orchestrator binds as Anthropic-format tools. They live
behind the same BaseTool contract as Jira/GitLab tools, so the agent loop
and the registry don't know which provider is underneath.

post_dm and post_standup_question are writers (`requires_approval=True`);
get_messages is read-only and exposed to Phase 5.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel, Field

from tl_agent.models.chat import ChatMessage, PostResult
from tl_agent.tools.base import BaseTool, RetryPolicy
from tl_agent.tools.chat.factory import get_chat_provider
from tl_agent.tools.idempotency import make_key
from tl_agent.tools.registry import registry

# -------------------- post_dm --------------------


class PostDMIn(BaseModel):
    user_id: str = Field(min_length=1)
    body: str = Field(min_length=1, max_length=2000)


class PostDMTool(BaseTool[PostDMIn, PostResult]):
    name: ClassVar[str] = "post_dm"
    description: ClassVar[str] = (
        "Send a direct message via the configured chat provider. Side-effecting; "
        "only invoked after Phase 8 TL approval."
    )
    input_model: ClassVar[type[BaseModel]] = PostDMIn
    output_model: ClassVar[type[BaseModel]] = PostResult
    requires_approval: ClassVar[bool] = True
    retry_policy: ClassVar[RetryPolicy] = RetryPolicy(max_attempts=2, initial_delay_ms=200)

    def idempotency_key(self, args: PostDMIn, *, run_date_iso: str) -> str | None:
        return make_key(tool_name=self.name, args=args, run_date_iso=run_date_iso)

    async def _call(self, args: PostDMIn) -> PostResult:
        provider = get_chat_provider()
        return await provider.post_dm(user_id=args.user_id, body=args.body)


# -------------------- post_standup_question --------------------


class PostStandupQuestionIn(BaseModel):
    channel_id: str = Field(min_length=1)
    body: str = Field(min_length=1, max_length=2000)


class PostStandupQuestionTool(BaseTool[PostStandupQuestionIn, PostResult]):
    name: ClassVar[str] = "post_standup_question"
    description: ClassVar[str] = (
        "Post a question into the team's standup channel. Side-effecting; "
        "only invoked after Phase 8 TL approval."
    )
    input_model: ClassVar[type[BaseModel]] = PostStandupQuestionIn
    output_model: ClassVar[type[BaseModel]] = PostResult
    requires_approval: ClassVar[bool] = True
    retry_policy: ClassVar[RetryPolicy] = RetryPolicy(max_attempts=2, initial_delay_ms=200)

    def idempotency_key(self, args: PostStandupQuestionIn, *, run_date_iso: str) -> str | None:
        return make_key(tool_name=self.name, args=args, run_date_iso=run_date_iso)

    async def _call(self, args: PostStandupQuestionIn) -> PostResult:
        provider = get_chat_provider()
        return await provider.post_channel(channel_id=args.channel_id, body=args.body)


# -------------------- get_chat_messages (read-only) --------------------


class GetChatMessagesIn(BaseModel):
    channel_id: str = Field(min_length=1)
    since: datetime
    until: datetime
    limit: int = Field(default=100, ge=1, le=500)


class GetChatMessagesOut(BaseModel):
    messages: list[ChatMessage] = Field(default_factory=list[ChatMessage])


class GetChatMessagesTool(BaseTool[GetChatMessagesIn, GetChatMessagesOut]):
    name: ClassVar[str] = "get_chat_messages"
    description: ClassVar[str] = (
        "Fetch chat-channel history in a time window. Used by Phase 1 standup "
        "collection and by readback after writers."
    )
    input_model: ClassVar[type[BaseModel]] = GetChatMessagesIn
    output_model: ClassVar[type[BaseModel]] = GetChatMessagesOut

    async def _call(self, args: GetChatMessagesIn) -> GetChatMessagesOut:
        provider = get_chat_provider()
        msgs = await provider.get_messages(
            channel_id=args.channel_id, since=args.since, until=args.until, limit=args.limit
        )
        return GetChatMessagesOut(messages=list(msgs))


# -------------------- registration --------------------


def register_chat_tools() -> None:
    """Add chat tools to the registry. Idempotent."""
    import contextlib

    from tl_agent.tools.registry import RegistryError

    for tool_cls in (PostDMTool, PostStandupQuestionTool, GetChatMessagesTool):
        with contextlib.suppress(RegistryError):
            registry.register(tool_cls())

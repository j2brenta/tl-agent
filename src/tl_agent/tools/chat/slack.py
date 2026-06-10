"""Slack ChatProvider — STUB adapter.

This file is real code behind the same `ChatProvider` interface. It is NOT
test-gated because we don't seed a Slack workspace in CI. To flip Slack to
primary:

  1. Set `TLA_CHAT_PROVIDER=slack` in env.
  2. Set `TLA_SLACK_BOT_TOKEN=xoxb-...` and `TLA_SLACK_SIGNING_SECRET=...`.
  3. Remove the `pytest.mark.skipif(...)` on tests/integration/test_chat_slack.py
     (planned, not yet seeded).

What this exists to prove:
  - The ChatProvider abstraction is real (a second adapter compiles + runs).
  - We have thought about Slack-specific gotchas — signing secret + replay
    window, conversations.open for DMs, conversations.history pagination.

What this DOES NOT do:
  - Drive any test today. Drift is monitored via `make check` (the file
    must type-check) but no behaviour assertion lives here.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import UTC, datetime
from typing import Any, cast

from slack_sdk.web.async_client import AsyncWebClient

from tl_agent.models.chat import ChatMessage, PostResult
from tl_agent.settings import get_settings
from tl_agent.tools.chat.base import (
    ChatProvider,
    OAuthState,
    constant_time_equals,
    store_token,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "slack"
_REPLAY_WINDOW_SECONDS = 60 * 5  # 5 minutes — Slack's recommended window


class SlackProvider(ChatProvider):
    """Slack adapter — stub, behind the same ChatProvider interface."""

    name = PROVIDER_NAME

    def __init__(self) -> None:
        s = get_settings()
        self._client = AsyncWebClient(token=s.slack_bot_token)
        self._signing_secret = s.slack_signing_secret.encode("utf-8")

    # ---------- client helpers ----------
    # The AsyncWebClient methods carry `**kwargs: Unknown` in their stubs, which
    # trips pyright strict's reportUnknownMemberType at every call site. Funnel
    # them through explicitly-typed helpers (one documented ignore each) so the
    # global check stays live for the rest of the module.

    async def _conversations_open(self, *, users: list[str]) -> Any:
        return await self._client.conversations_open(users=users)  # pyright: ignore[reportUnknownMemberType]

    async def _post_message(self, *, channel: str, text: str) -> Any:
        return await self._client.chat_postMessage(channel=channel, text=text)  # pyright: ignore[reportUnknownMemberType]

    async def _conversations_history(self, **kwargs: Any) -> Any:
        return await self._client.conversations_history(**kwargs)  # pyright: ignore[reportUnknownMemberType]

    # ---------- post ----------

    async def post_dm(self, *, user_id: str, body: str) -> PostResult:
        open_resp = await self._conversations_open(users=[user_id])
        channel = cast(dict[str, Any], open_resp.get("channel") or {})
        channel_id = str(channel["id"])
        return await self.post_channel(channel_id=channel_id, body=body)

    async def post_channel(self, *, channel_id: str, body: str) -> PostResult:
        resp = await self._post_message(channel=channel_id, text=body)
        ts = str(resp["ts"])
        return PostResult(
            provider=self.name,
            message_id=ts,
            channel_id=channel_id,
            posted_at=datetime.fromtimestamp(float(ts), tz=UTC),
            body=body,
        )

    # ---------- read ----------

    async def get_messages(
        self, *, channel_id: str, since: datetime, until: datetime, limit: int = 100
    ) -> list[ChatMessage]:
        resp = await self._conversations_history(
            channel=channel_id,
            oldest=str(since.timestamp()),
            latest=str(until.timestamp()),
            limit=limit,
        )
        messages: list[dict[str, Any]] = list(resp.get("messages", []))
        return [_to_message(m, channel_id) for m in messages]

    async def get_message(self, *, channel_id: str, message_id: str) -> ChatMessage | None:
        resp = await self._conversations_history(
            channel=channel_id, latest=message_id, inclusive=True, limit=1
        )
        messages: list[dict[str, Any]] = list(resp.get("messages", []))
        if not messages:
            return None
        return _to_message(messages[0], channel_id)

    # ---------- signature ----------

    def verify_webhook_signature(
        self, *, body: bytes, signature: str, timestamp: str | None = None
    ) -> bool:
        """Slack signature scheme: v0=HMAC-SHA256(secret, f"v0:{ts}:{body}").

        Includes replay-window enforcement — anything older than 5 minutes
        is rejected even if the HMAC matches.
        """
        if not timestamp:
            return False
        try:
            ts_int = int(timestamp)
        except ValueError:
            return False
        if abs(int(datetime.now(UTC).timestamp()) - ts_int) > _REPLAY_WINDOW_SECONDS:
            return False
        basestring = f"v0:{timestamp}:".encode() + body
        expected = "v0=" + hmac.new(self._signing_secret, basestring, hashlib.sha256).hexdigest()
        return constant_time_equals(expected, signature)

    # ---------- oauth ----------

    async def refresh_oauth_token(self) -> OAuthState:
        """Slack bot tokens don't expire — return the static one wrapped.

        For Slack user-token rotation (a different flow), call
        oauth.v2.access with grant_type=refresh_token. Not exercised here.
        """
        state = OAuthState(access_token=get_settings().slack_bot_token)
        store_token(state)
        return state


def _to_message(m: dict[str, Any], channel_id: str) -> ChatMessage:
    return ChatMessage(
        id=str(m.get("ts", "")),
        channel_id=channel_id,
        user_id=str(m.get("user", "") or m.get("bot_id", "")),
        created_at=datetime.fromtimestamp(float(m["ts"]), tz=UTC),
        text=str(m.get("text", "")),
        thread_id=str(m.get("thread_ts")) if m.get("thread_ts") else None,
        edited_at=datetime.fromtimestamp(float(m["edited"]["ts"]), tz=UTC)
        if m.get("edited")
        else None,
    )

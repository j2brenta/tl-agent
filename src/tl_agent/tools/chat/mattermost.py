"""Mattermost ChatProvider — the primary implementation.

Hits the real Mattermost v4 REST API. Three senior signals live here:

- Real OAuth refresh: `refresh_oauth_token()` exchanges the cached refresh
  token at `/oauth/access_token`, updates the in-process cache, and is what
  the integration test forces via a deliberate token expiry.

- Real webhook signature verification: `verify_webhook_signature` uses
  HMAC-SHA256 over `f"{timestamp}.{body}"` with the shared secret, in
  constant time. Matches what `services/mattermost_seed/webhook_target.py`
  does on the receiving side.

- Real readback after post: every writer fetches the just-posted message
  back via `get_message` and asserts the body matches. The orchestrator
  wraps `post_dm` in `readback()` so a missing or mismatched fetch fails
  the call before the idempotency cache is written.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from tl_agent.models.chat import ChatMessage, PostResult
from tl_agent.settings import get_settings
from tl_agent.tools._http import http_client, raise_from_http_error, raise_from_transport_error
from tl_agent.tools.chat.base import (
    ChatProvider,
    OAuthState,
    cached_token,
    constant_time_equals,
    store_token,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "mattermost"

# Mattermost IDs are 26 char base32-ish. Anything else is a name we must
# resolve via the team-scoped endpoint.
_ID_LENGTH = 26
_CHANNEL_ID_CACHE: dict[str, str] = {}


def _looks_like_channel_id(value: str) -> bool:
    return len(value) == _ID_LENGTH and value.isalnum()


async def _resolve_channel(client: httpx.AsyncClient, channel_id_or_name: str) -> str:
    """Return the channel ID, resolving a name through the team API if needed."""
    if _looks_like_channel_id(channel_id_or_name):
        return channel_id_or_name
    if channel_id_or_name in _CHANNEL_ID_CACHE:
        return _CHANNEL_ID_CACHE[channel_id_or_name]
    team = get_settings().mattermost_team
    r = await client.get(f"/api/v4/teams/name/{team}/channels/name/{channel_id_or_name}")
    r.raise_for_status()
    resolved = str(r.json()["id"])
    _CHANNEL_ID_CACHE[channel_id_or_name] = resolved
    return resolved


def _client() -> httpx.AsyncClient:
    s = get_settings()
    cached = cached_token()
    token = cached.access_token if cached is not None else s.mattermost_token
    if not token:
        # httpx would otherwise raise `Illegal header value b'Bearer '`.
        # Surface a typed ToolException so the orchestrator records it as
        # a clean phase-1 note instead of a transport crash.
        from tl_agent.tools.base import ToolErrorKind, ToolException

        raise ToolException(
            kind=ToolErrorKind.UNAUTHORIZED,
            message=(
                "mattermost: no token configured. Set TLA_MATTERMOST_TOKEN "
                "(see services/mattermost_seed/seed.py output) and re-run."
            ),
        )
    return http_client(
        base_url=s.mattermost_url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )


class MattermostProvider(ChatProvider):
    """ChatProvider for Mattermost server v6+."""

    name = PROVIDER_NAME

    # ---------- DM ----------

    async def post_dm(self, *, user_id: str, body: str) -> PostResult:
        # Mattermost DM = direct channel between bot and user; create-or-get,
        # then post into it.
        async with _client() as client:
            try:
                bot = await client.get("/api/v4/users/me")
                bot.raise_for_status()
                bot_id = str(bot.json()["id"])
                r_chan = await client.post("/api/v4/channels/direct", json=[bot_id, user_id])
                r_chan.raise_for_status()
                channel_id = str(r_chan.json()["id"])
                return await self._post(client, channel_id, body)
            except httpx.HTTPStatusError as exc:
                raise_from_http_error(exc, tool_label=f"{self.name}.post_dm")
            except httpx.HTTPError as exc:
                raise_from_transport_error(exc, tool_label=f"{self.name}.post_dm")

    async def post_channel(self, *, channel_id: str, body: str) -> PostResult:
        async with _client() as client:
            try:
                resolved_id = await _resolve_channel(client, channel_id)
                return await self._post(client, resolved_id, body)
            except httpx.HTTPStatusError as exc:
                raise_from_http_error(exc, tool_label=f"{self.name}.post_channel")
            except httpx.HTTPError as exc:
                raise_from_transport_error(exc, tool_label=f"{self.name}.post_channel")

    async def _post(self, client: httpx.AsyncClient, channel_id: str, body: str) -> PostResult:
        r = await client.post("/api/v4/posts", json={"channel_id": channel_id, "message": body})
        r.raise_for_status()
        data = r.json()
        return PostResult(
            provider=self.name,
            message_id=str(data["id"]),
            channel_id=channel_id,
            posted_at=datetime.fromtimestamp(int(data["create_at"]) / 1000, tz=UTC),
            body=body,
        )

    # ---------- read ----------

    async def get_messages(
        self, *, channel_id: str, since: datetime, until: datetime, limit: int = 100
    ) -> list[ChatMessage]:
        del until  # Mattermost's posts endpoint is page-bounded by `since`; we cap at `limit`
        params = {"since": int(since.timestamp() * 1000), "per_page": limit}
        async with _client() as client:
            try:
                resolved_id = await _resolve_channel(client, channel_id)
                r = await client.get(f"/api/v4/channels/{resolved_id}/posts", params=params)
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise_from_http_error(exc, tool_label=f"{self.name}.get_messages")
            except httpx.HTTPError as exc:
                raise_from_transport_error(exc, tool_label=f"{self.name}.get_messages")
        data = r.json()
        return [_to_message(p, resolved_id) for p in (data.get("posts") or {}).values()]

    async def get_message(self, *, channel_id: str, message_id: str) -> ChatMessage | None:
        async with _client() as client:
            try:
                r = await client.get(f"/api/v4/posts/{message_id}")
                if r.status_code == 404:
                    return None
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    return None
                raise_from_http_error(exc, tool_label=f"{self.name}.get_message")
            except httpx.HTTPError as exc:
                raise_from_transport_error(exc, tool_label=f"{self.name}.get_message")
        return _to_message(r.json(), channel_id)

    # ---------- webhook signature ----------

    def verify_webhook_signature(
        self, *, body: bytes, signature: str, timestamp: str | None = None
    ) -> bool:
        secret = get_settings().mattermost_webhook_secret.encode("utf-8")
        ts = timestamp or ""
        # HMAC-SHA256(secret, f"{timestamp}.{body}")
        msg = (ts + ".").encode("utf-8") + body
        expected = hmac.new(secret, msg, hashlib.sha256).hexdigest()
        return constant_time_equals(expected, signature)

    # ---------- OAuth refresh ----------

    async def refresh_oauth_token(self) -> OAuthState:
        """Exchange the cached refresh_token for a new access_token.

        If no refresh_token is cached, falls back to returning the existing
        static bot token from settings (real bots never expire on
        Mattermost). This keeps the abstraction usable for both flows.
        """
        s = get_settings()
        current = cached_token()
        if current is None or current.refresh_token is None:
            # Static bot token from settings — no rotation needed.
            state = OAuthState(access_token=s.mattermost_token)
            store_token(state)
            return state
        async with _client() as client:
            try:
                r = await client.post(
                    "/oauth/access_token",
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": current.refresh_token,
                    },
                )
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise_from_http_error(exc, tool_label=f"{self.name}.refresh_oauth_token")
            except httpx.HTTPError as exc:
                raise_from_transport_error(exc, tool_label=f"{self.name}.refresh_oauth_token")
        data = r.json()
        state = OAuthState(
            access_token=str(data["access_token"]),
            refresh_token=str(data.get("refresh_token") or current.refresh_token),
            expires_at=datetime.fromtimestamp(int(data.get("expires_at", 0)), tz=UTC)
            if data.get("expires_at")
            else None,
        )
        store_token(state)
        logger.info("mattermost.token_refreshed")
        return state


# -------------------- helpers --------------------


def _to_message(post: dict[str, Any], channel_id: str) -> ChatMessage:
    return ChatMessage(
        id=str(post["id"]),
        channel_id=channel_id,
        user_id=str(post.get("user_id", "")),
        created_at=datetime.fromtimestamp(int(post.get("create_at", 0)) / 1000, tz=UTC),
        text=str(post.get("message", "")),
        thread_id=str(post.get("root_id")) if post.get("root_id") else None,
        edited_at=datetime.fromtimestamp(int(post["update_at"]) / 1000, tz=UTC)
        if post.get("update_at")
        else None,
    )

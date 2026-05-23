"""ChatProvider ABC.

The whole point of this module is the abstraction. Mattermost is the primary
implementation (real REST + webhook signature verification + OAuth refresh
against the in-compose Mattermost server). Slack is a stub adapter behind
the same six-method interface — its code is real and committed, but it is
not gated by integration tests because we do not seed a real workspace.

The agent never imports a concrete provider. It calls `get_chat_provider()`
which reads `settings.chat_provider` and returns the configured instance.
"""

from __future__ import annotations

import hmac
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from tl_agent.models.chat import ChatMessage, PostResult


@dataclass(frozen=True)
class OAuthState:
    """Token state. `refresh_token` is None for bot tokens that never expire."""

    access_token: str
    expires_at: datetime | None = None
    refresh_token: str | None = None


@dataclass
class _TokenStore:
    """In-process cache for OAuthState — overridden in tests."""

    state: OAuthState | None = None


class ChatProvider(ABC):
    """The six-method contract.

    Implementations:
      - MattermostProvider (primary; real REST + signed webhooks + OAuth refresh)
      - SlackProvider      (stub adapter; real SDK calls, not test-gated)

    Why this shape: it's the smallest surface that lets us exercise the four
    senior signals (real POST, real GET-back readback, real OAuth refresh,
    real webhook signature verification) without scope creep.
    """

    name: str

    @abstractmethod
    async def post_dm(self, *, user_id: str, body: str) -> PostResult:
        """Send a direct message to one user. Used by Phase 8 DM mode."""

    @abstractmethod
    async def post_channel(self, *, channel_id: str, body: str) -> PostResult:
        """Post a message to a channel. Used by Phase 8 STANDUP/ESCALATE modes."""

    @abstractmethod
    async def get_messages(
        self, *, channel_id: str, since: datetime, until: datetime, limit: int = 100
    ) -> list[ChatMessage]:
        """Fetch channel history in a time window. Used by Phase 1 + readback."""

    @abstractmethod
    async def get_message(self, *, channel_id: str, message_id: str) -> ChatMessage | None:
        """Fetch a specific message — primary use is readback after post."""

    @abstractmethod
    def verify_webhook_signature(
        self, *, body: bytes, signature: str, timestamp: str | None = None
    ) -> bool:
        """Verify an inbound outgoing-webhook payload's signature.

        Used by the `services/mattermost_seed/webhook_target.py` receiver
        when Mattermost forwards a channel event to us. Must be constant-time.
        """

    @abstractmethod
    async def refresh_oauth_token(self) -> OAuthState:
        """Exchange the refresh token for a new access token. Idempotent."""


# -------------------- helpers shared by adapters --------------------


def constant_time_equals(a: str, b: str) -> bool:
    """Wrapper around hmac.compare_digest for clarity at call sites."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


_TOKEN_STORE: _TokenStore = _TokenStore()


def store_token(state: OAuthState) -> None:
    """Cache the current OAuth state in-process (across calls in one run)."""
    _TOKEN_STORE.state = state


def cached_token() -> OAuthState | None:
    return _TOKEN_STORE.state

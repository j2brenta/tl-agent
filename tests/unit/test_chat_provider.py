"""Chat provider tests — abstraction + signature verification + factory.

We DON'T hit Mattermost or Slack in unit tests. Two unit-level concerns:
  1. Both adapters implement every ChatProvider method (interface conformance).
  2. Signature verification (Mattermost HMAC + Slack v0 scheme) is correct,
     including replay-window rejection for Slack.

Mattermost integration (real REST) lives in tests/integration/ once compose
is up.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import UTC, datetime

import httpx
import pytest
from pytest_httpx import HTTPXMock

from tl_agent.settings import get_settings
from tl_agent.tools import ToolResult, registry
from tl_agent.tools.chat import (
    ChatProvider,
    GetChatMessagesTool,
    MattermostProvider,
    PostDMTool,
    SlackProvider,
    get_chat_provider,
    register_chat_tools,
    reset_provider_cache,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    registry.reset()
    reset_provider_cache()


# -------------------- interface conformance --------------------


@pytest.mark.parametrize("cls", [MattermostProvider, SlackProvider])
def test_implements_full_interface(cls: type[ChatProvider]) -> None:
    instance = cls()
    for method in (
        "post_dm",
        "post_channel",
        "get_messages",
        "get_message",
        "verify_webhook_signature",
        "refresh_oauth_token",
    ):
        assert callable(getattr(instance, method))
    assert isinstance(instance.name, str) and instance.name


# -------------------- factory --------------------


def test_factory_returns_mattermost_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TLA_CHAT_PROVIDER", "mattermost")
    p = get_chat_provider()
    assert isinstance(p, MattermostProvider)
    # Same instance second time
    assert get_chat_provider() is p


def test_factory_returns_slack_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TLA_CHAT_PROVIDER", "slack")
    p = get_chat_provider()
    assert isinstance(p, SlackProvider)


def test_factory_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TLA_CHAT_PROVIDER", "telegram")
    with pytest.raises(Exception):  # noqa: B017 — pydantic literal_error OR our RuntimeError
        get_chat_provider()


# -------------------- mattermost signature verification --------------------


def test_mattermost_sig_passes_with_correct_hmac() -> None:
    secret = get_settings().mattermost_webhook_secret.encode("utf-8")
    body = b'{"event":"posted"}'
    ts = "1716368400"
    msg = (ts + ".").encode() + body
    sig = hmac.new(secret, msg, hashlib.sha256).hexdigest()
    assert MattermostProvider().verify_webhook_signature(body=body, signature=sig, timestamp=ts)


def test_mattermost_sig_fails_with_bad_hmac() -> None:
    assert not MattermostProvider().verify_webhook_signature(
        body=b"x", signature="0" * 64, timestamp="1716368400"
    )


# -------------------- slack signature --------------------


def test_slack_sig_passes_with_fresh_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TLA_SLACK_SIGNING_SECRET", "shhh")
    ts = str(int(time.time()))
    body = b'{"event":"message"}'
    basestring = f"v0:{ts}:".encode() + body
    expected_sig = "v0=" + hmac.new(b"shhh", basestring, hashlib.sha256).hexdigest()
    assert SlackProvider().verify_webhook_signature(body=body, signature=expected_sig, timestamp=ts)


def test_slack_sig_rejects_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TLA_SLACK_SIGNING_SECRET", "shhh")
    # Stale timestamp (> 5 min old)
    ts = str(int(time.time()) - 3600)
    body = b'{"event":"message"}'
    basestring = f"v0:{ts}:".encode() + body
    sig = "v0=" + hmac.new(b"shhh", basestring, hashlib.sha256).hexdigest()
    assert not SlackProvider().verify_webhook_signature(body=body, signature=sig, timestamp=ts)


def test_slack_sig_rejects_missing_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TLA_SLACK_SIGNING_SECRET", "shhh")
    assert not SlackProvider().verify_webhook_signature(
        body=b"x", signature="v0=...", timestamp=None
    )


# -------------------- post_dm tool (mattermost over httpx stub) --------------------


async def test_post_dm_tool_goes_through_mattermost(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TLA_CHAT_PROVIDER", "mattermost")
    monkeypatch.setenv("TLA_MATTERMOST_TOKEN", "test-bot-token")
    register_chat_tools()
    httpx_mock.add_response(url="http://localhost:8065/api/v4/users/me", json={"id": "bot-1"})
    httpx_mock.add_response(
        url="http://localhost:8065/api/v4/channels/direct",
        method="POST",
        json={"id": "chan-direct-1"},
    )
    httpx_mock.add_response(
        url="http://localhost:8065/api/v4/posts",
        method="POST",
        json={"id": "msg-1", "create_at": 1716368400000},
    )
    result = await PostDMTool().invoke({"user_id": "john", "body": "hi"}, run_date_iso="2026-05-22")
    assert isinstance(result, ToolResult)
    assert result.value.message_id == "msg-1"
    assert result.value.provider == "mattermost"


async def test_get_chat_messages_tool(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TLA_CHAT_PROVIDER", "mattermost")
    monkeypatch.setenv("TLA_MATTERMOST_TOKEN", "test-bot-token")
    since_dt = datetime(2026, 5, 22, 9, 0, tzinfo=UTC)
    since_ms = int(since_dt.timestamp() * 1000)
    httpx_mock.add_response(
        url=httpx.URL(
            "http://localhost:8065/api/v4/channels/chan-1/posts",
            params={"since": since_ms, "per_page": 100},
        ),
        json={
            "posts": {
                "m1": {
                    "id": "m1",
                    "user_id": "u-john",
                    "create_at": since_ms + 60_000,
                    "message": "today: ENG-12 wip",
                }
            }
        },
    )
    result = await GetChatMessagesTool().invoke(
        {
            "channel_id": "chan-1",
            "since": since_dt.isoformat(),
            "until": "2026-05-22T10:00:00+00:00",
        },
        run_date_iso="2026-05-22",
    )
    assert isinstance(result, ToolResult)
    assert len(result.value.messages) == 1
    assert result.value.messages[0].text.startswith("today:")


# -------------------- empty token guard --------------------


async def test_mattermost_empty_token_returns_clean_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """No bearer token configured ⇒ ToolError, not httpx 'Illegal header value' crash."""
    from tl_agent.tools import ToolError, ToolErrorKind
    from tl_agent.tools.chat import base as chat_base

    monkeypatch.setenv("TLA_CHAT_PROVIDER", "mattermost")
    monkeypatch.setenv("TLA_MATTERMOST_TOKEN", "")
    chat_base._TOKEN_STORE.state = None
    register_chat_tools()
    result = await GetChatMessagesTool().invoke(
        {
            "channel_id": "c",
            "since": datetime(2026, 5, 22, 9, 0, tzinfo=UTC).isoformat(),
            "until": datetime(2026, 5, 22, 10, 0, tzinfo=UTC).isoformat(),
        },
        run_date_iso="2026-05-22",
    )
    assert isinstance(result, ToolError)
    assert result.kind is ToolErrorKind.UNAUTHORIZED
    assert "TLA_MATTERMOST_TOKEN" in result.message


# -------------------- registration --------------------


def test_register_chat_tools() -> None:
    register_chat_tools()
    names = {t.name for t in registry.all()}
    assert {"post_dm", "post_standup_question", "get_chat_messages"} <= names
    # Writers excluded from phase 5
    phase5_names = {t.name for t in registry.for_phase5()}
    assert "post_dm" not in phase5_names
    assert "get_chat_messages" in phase5_names


# -------------------- oauth refresh (mattermost, mocked) --------------------


async def test_mattermost_oauth_refresh_returns_static_token_for_bots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If no refresh_token cached, fall back to the static bot token."""
    monkeypatch.setenv("TLA_MATTERMOST_TOKEN", "bot-static-token")
    # Important: get_settings() reads env fresh, but the cached _TOKEN_STORE
    # might still hold a previous state — reset it via store_token(None) impossible
    # so just construct fresh and call.
    from tl_agent.tools.chat import base as chat_base

    chat_base._TOKEN_STORE.state = None
    state = await MattermostProvider().refresh_oauth_token()
    assert state.access_token == "bot-static-token"
    assert state.refresh_token is None


async def test_mattermost_oauth_refresh_with_refresh_token(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a refresh_token is cached, POST /oauth/access_token and update state."""
    from tl_agent.tools.chat import base as chat_base

    chat_base._TOKEN_STORE.state = chat_base.OAuthState(
        access_token="old-access",
        refresh_token="rt-1",
        expires_at=datetime(2026, 5, 22, 0, 0, tzinfo=UTC),
    )
    httpx_mock.add_response(
        url="http://localhost:8065/oauth/access_token",
        method="POST",
        json={"access_token": "new-access", "refresh_token": "rt-2", "expires_at": 9999999999},
    )
    state = await MattermostProvider().refresh_oauth_token()
    assert state.access_token == "new-access"
    assert state.refresh_token == "rt-2"

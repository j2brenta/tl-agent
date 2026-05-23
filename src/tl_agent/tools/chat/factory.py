"""Chat provider factory.

Reads `settings.chat_provider` once per process and returns a single shared
instance. The orchestrator + Phase 8 executor + the integration tests all
call `get_chat_provider()`.

Provider selection is a config-only change: flip `TLA_CHAT_PROVIDER=slack`
(plus the SLACK_* env vars) and every call site picks up the new backend
without code changes. That's the abstraction's value proposition.
"""

from __future__ import annotations

import logging

from tl_agent.settings import get_settings
from tl_agent.tools.chat.base import ChatProvider
from tl_agent.tools.chat.mattermost import MattermostProvider
from tl_agent.tools.chat.slack import SlackProvider

logger = logging.getLogger(__name__)

_INSTANCES: dict[str, ChatProvider] = {}


def get_chat_provider() -> ChatProvider:
    """Return the configured `ChatProvider`. Memoised per process."""
    name = get_settings().chat_provider
    if name in _INSTANCES:
        return _INSTANCES[name]
    provider: ChatProvider
    if name == "mattermost":
        provider = MattermostProvider()
    elif name == "slack":
        provider = SlackProvider()
    else:
        raise RuntimeError(f"unknown chat provider: {name!r}")
    _INSTANCES[name] = provider
    logger.info("chat.provider_selected", extra={"provider": name})
    return provider


def reset_provider_cache() -> None:
    """Drop the memoised instance — for tests."""
    _INSTANCES.clear()

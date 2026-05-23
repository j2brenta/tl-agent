"""Chat layer — provider-agnostic interface, Mattermost primary, Slack stub."""

from tl_agent.tools.chat.base import ChatProvider, OAuthState
from tl_agent.tools.chat.factory import get_chat_provider, reset_provider_cache
from tl_agent.tools.chat.mattermost import MattermostProvider
from tl_agent.tools.chat.slack import SlackProvider
from tl_agent.tools.chat.tools import (
    GetChatMessagesTool,
    PostDMTool,
    PostStandupQuestionTool,
    register_chat_tools,
)

__all__ = [
    "ChatProvider",
    "GetChatMessagesTool",
    "MattermostProvider",
    "OAuthState",
    "PostDMTool",
    "PostStandupQuestionTool",
    "SlackProvider",
    "get_chat_provider",
    "register_chat_tools",
    "reset_provider_cache",
]

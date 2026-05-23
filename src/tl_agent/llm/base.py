"""Provider contract.

Every LLM provider implements the same `Provider` ABC. Phases never import a
concrete provider; they call `router.for_phase(...).complete(...)`. This is
what lets us run a real eval-time A/B by flipping `config/router.yaml`.

Two call shapes:
- `complete(...)` — multi-turn with optional tool calling; used by Phase 5 ReACT
- `structured(...)` — single call returning a Pydantic model validated against
  the JSON schema we sent; used by Phases 2/3/6 where output shape is fixed
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel

# -------------------- message + content blocks --------------------


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True)
class ToolUseBlock:
    """An LLM asking us to call a tool. Emitted by the assistant role."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolResultBlock:
    """Our reply to a tool_use, carried back in the next user message."""

    tool_use_id: str
    content: str  # JSON-encoded result payload, or error message
    is_error: bool = False


@dataclass(frozen=True)
class Message:
    """One conversational turn.

    For simple turns, `content` is a string. For tool flows, `tool_uses` or
    `tool_results` carry the structured blocks alongside any text content.
    """

    role: MessageRole
    content: str = ""
    tool_uses: tuple[ToolUseBlock, ...] = ()
    tool_results: tuple[ToolResultBlock, ...] = ()


# -------------------- request / response --------------------


StopReason = Literal["end_turn", "tool_use", "max_tokens", "stop_sequence", "refusal"]


@dataclass(frozen=True)
class TokenUsage:
    """Per-call token + cost accounting."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class CompletionRequest:
    """Inputs to `Provider.complete`."""

    model: str
    messages: tuple[Message, ...]
    system: str | None = None
    max_tokens: int = 1024
    temperature: float = 0.0
    tools: tuple[dict[str, Any], ...] = ()  # Anthropic-format schemas from registry
    tool_choice: Literal["auto", "any", "none"] = "auto"
    cache_system: bool = False  # set true to mark system block cacheable
    phase: str | None = None  # for spans only


@dataclass(frozen=True)
class CompletionResponse:
    """Output from `Provider.complete`."""

    text: str
    tool_uses: tuple[ToolUseBlock, ...]
    stop_reason: StopReason
    usage: TokenUsage
    raw: dict[str, Any] = field(default_factory=dict[str, Any])  # provider-native payload


# -------------------- provider ABC --------------------


class Provider(ABC):
    """The contract every LLM provider implements."""

    name: str

    @abstractmethod
    async def complete(self, req: CompletionRequest) -> CompletionResponse:
        """Multi-turn completion with optional tool calling."""

    @abstractmethod
    async def structured[T: BaseModel](
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: type[T],
        max_tokens: int = 1024,
        temperature: float = 0.0,
        cache_system: bool = False,
        phase: str | None = None,
    ) -> tuple[T, TokenUsage]:
        """Single call returning a Pydantic-validated structured output.

        Implementations should constrain the LLM via tool-use or JSON-mode,
        validate the response with `schema.model_validate_json`, and surface
        any validation error as ProviderError.
        """

    @abstractmethod
    def estimate_tokens(self, text: str) -> int:
        """Cheap, deterministic token estimate. Used for budget gates."""


class ProviderError(RuntimeError):
    """LLM provider failed in a way the caller should see."""

    def __init__(self, message: str, *, retriable: bool = False) -> None:
        super().__init__(message)
        self.retriable = retriable

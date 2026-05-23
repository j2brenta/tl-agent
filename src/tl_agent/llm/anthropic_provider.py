"""Anthropic provider.

Wraps the official `anthropic` SDK and emits the canonical LLM span attributes
(tokens.in/out/cache_read, cost_usd, cache_hit, latency_ms). Supports prompt
caching when `cache_system=True` is set on the request.

Pricing table is hard-coded per model — keep in sync with public pricing.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, cast

from anthropic import APIStatusError, AsyncAnthropic
from anthropic.types import (
    MessageParam,
    TextBlockParam,
    ToolResultBlockParam,
    ToolUseBlockParam,
)
from pydantic import BaseModel, ValidationError

from tl_agent.llm.base import (
    CompletionRequest,
    CompletionResponse,
    Message,
    MessageRole,
    Provider,
    ProviderError,
    StopReason,
    TokenUsage,
    ToolUseBlock,
)
from tl_agent.obs.spans import llm_span, set_llm_attrs

if TYPE_CHECKING:
    from anthropic.types import Message as AnthMessage

logger = logging.getLogger(__name__)


# Per-million-token USD pricing — keep in sync with anthropic.com/pricing.
# Tuple: (input_per_mtok, output_per_mtok, cache_read_per_mtok, cache_write_per_mtok)
PRICING: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-4-7": (15.00, 75.00, 1.50, 18.75),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30, 3.75),
    "claude-haiku-4-5": (0.80, 4.00, 0.08, 1.00),
}


def _price(model: str, usage_dict: dict[str, int]) -> float:
    pricing = PRICING.get(model)
    if not pricing:
        return 0.0
    inp, outp, cache_r, cache_w = pricing
    in_t = usage_dict.get("input_tokens", 0)
    out_t = usage_dict.get("output_tokens", 0)
    cr_t = usage_dict.get("cache_read_input_tokens", 0)
    cw_t = usage_dict.get("cache_creation_input_tokens", 0)
    return (
        in_t * inp / 1_000_000
        + out_t * outp / 1_000_000
        + cr_t * cache_r / 1_000_000
        + cw_t * cache_w / 1_000_000
    )


class AnthropicProvider(Provider):
    """Anthropic Messages API wrapper."""

    name = "anthropic"

    def __init__(self, *, api_key: str, base_url: str | None = None) -> None:
        self._client = AsyncAnthropic(api_key=api_key, base_url=base_url)

    # ---------- complete ----------

    async def complete(self, req: CompletionRequest) -> CompletionResponse:
        anth_messages = _to_anthropic_messages(req.messages)
        system_param = _system_param(req.system, cache=req.cache_system)
        tools_param: list[dict[str, Any]] = list(req.tools) if req.tools else []

        with llm_span(req.model, phase=req.phase):
            start = time.perf_counter()
            try:
                msg = await self._client.messages.create(
                    model=req.model,
                    messages=anth_messages,
                    system=system_param,
                    max_tokens=req.max_tokens,
                    temperature=req.temperature,
                    tools=cast(Any, tools_param) if tools_param else cast(Any, []),
                    tool_choice=cast(Any, {"type": req.tool_choice})
                    if tools_param
                    else cast(Any, {"type": "auto"}),
                )
            except APIStatusError as exc:
                raise ProviderError(
                    f"anthropic API error: {exc.status_code} {exc.message}",
                    retriable=exc.status_code in {429, 500, 502, 503, 504},
                ) from exc
            latency_ms = (time.perf_counter() - start) * 1000

            usage = _to_token_usage(msg, req.model)
            set_llm_attrs(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cost_usd=usage.cost_usd,
                cache_hit=usage.cache_read_tokens > 0,
                latency_ms=latency_ms,
            )

        text, tool_uses = _parse_response_blocks(msg)
        return CompletionResponse(
            text=text,
            tool_uses=tool_uses,
            stop_reason=cast(StopReason, msg.stop_reason or "end_turn"),
            usage=usage,
            raw=msg.model_dump(mode="json"),
        )

    # ---------- structured ----------

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
        """Constrain output via a tool-call; validate the captured args."""
        tool_name = f"return_{schema.__name__.lower()}"
        tool_def: dict[str, Any] = {
            "name": tool_name,
            "description": f"Return a {schema.__name__} object.",
            "input_schema": schema.model_json_schema(),
        }
        req = CompletionRequest(
            model=model,
            messages=(Message(role=MessageRole.USER, content=user),),
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=(tool_def,),
            tool_choice="any",
            cache_system=cache_system,
            phase=phase,
        )
        resp = await self.complete(req)
        if not resp.tool_uses:
            raise ProviderError("structured: model returned no tool_use block")
        block = resp.tool_uses[0]
        try:
            value = schema.model_validate(block.input)
        except ValidationError as exc:
            raise ProviderError(f"structured validation failed: {exc}") from exc
        return value, resp.usage

    # ---------- estimate ----------

    def estimate_tokens(self, text: str) -> int:
        # SDK exposes the tokenizer, but the 4-char approximation is accurate
        # enough for budget gates and adds no network call.
        return max(1, len(text) // 4)


# ============================ helpers ============================


def _system_param(system: str | None, *, cache: bool) -> Any:
    """Build the system parameter; emit a cache_control block when requested."""
    if not system:
        return ""
    if not cache:
        return system
    return [
        TextBlockParam(
            type="text",
            text=system,
            cache_control={"type": "ephemeral"},
        )
    ]


def _to_anthropic_messages(messages: tuple[Message, ...]) -> list[MessageParam]:
    out: list[MessageParam] = []
    for m in messages:
        if m.role is MessageRole.USER:
            content_blocks: list[Any] = []
            if m.content:
                content_blocks.append(TextBlockParam(type="text", text=m.content))
            content_blocks.extend(
                ToolResultBlockParam(
                    type="tool_result",
                    tool_use_id=tr.tool_use_id,
                    content=tr.content,
                    is_error=tr.is_error,
                )
                for tr in m.tool_results
            )
            out.append(MessageParam(role="user", content=content_blocks))
        elif m.role is MessageRole.ASSISTANT:
            content_blocks2: list[Any] = []
            if m.content:
                content_blocks2.append(TextBlockParam(type="text", text=m.content))
            content_blocks2.extend(
                ToolUseBlockParam(type="tool_use", id=tu.id, name=tu.name, input=tu.input)
                for tu in m.tool_uses
            )
            out.append(MessageParam(role="assistant", content=content_blocks2))
        elif m.role is MessageRole.TOOL:
            # Anthropic uses user-role messages with tool_result blocks. We
            # accept role=TOOL in our abstraction for clarity and translate.
            tool_blocks: list[Any] = [
                ToolResultBlockParam(
                    type="tool_result",
                    tool_use_id=tr.tool_use_id,
                    content=tr.content,
                    is_error=tr.is_error,
                )
                for tr in m.tool_results
            ]
            out.append(MessageParam(role="user", content=tool_blocks))
    return out


def _parse_response_blocks(msg: AnthMessage) -> tuple[str, tuple[ToolUseBlock, ...]]:
    text_parts: list[str] = []
    tool_uses: list[ToolUseBlock] = []
    for block in msg.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_uses.append(
                ToolUseBlock(
                    id=block.id,
                    name=block.name,
                    input=cast(dict[str, Any], block.input) if block.input else {},
                )
            )
    return "\n".join(text_parts), tuple(tool_uses)


def _to_token_usage(msg: AnthMessage, model: str) -> TokenUsage:
    raw = msg.usage.model_dump() if msg.usage else {}
    return TokenUsage(
        input_tokens=raw.get("input_tokens", 0),
        output_tokens=raw.get("output_tokens", 0),
        cache_read_tokens=raw.get("cache_read_input_tokens", 0) or 0,
        cache_creation_tokens=raw.get("cache_creation_input_tokens", 0) or 0,
        cost_usd=_price(model, raw),
    )

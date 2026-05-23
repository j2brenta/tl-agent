"""Ollama provider.

Ollama exposes an OpenAI-compatible REST API. We use httpx directly rather
than pull in `openai` — the surface we need is small (chat/completions with
tool-calls) and the dependency would otherwise be unused.

Limited compared to Anthropic:
- No prompt caching.
- Tool-call support depends on model (works on llama 3.x with the chat
  template); we still ship the same interface so phases work end-to-end
  during offline eval iteration.

Pricing is set to 0.0 — local inference has no per-token cost.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx
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

logger = logging.getLogger(__name__)


class OllamaProvider(Provider):
    """Ollama via its OpenAI-compatible `/v1/chat/completions` endpoint."""

    name = "ollama"

    def __init__(self, *, base_url: str, timeout_seconds: float = 60.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout_seconds)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------- complete ----------

    async def complete(self, req: CompletionRequest) -> CompletionResponse:
        payload = _build_payload(req)
        with llm_span(req.model, phase=req.phase):
            start = time.perf_counter()
            try:
                resp = await self._client.post(f"{self._base_url}/chat/completions", json=payload)
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ProviderError(
                    f"ollama HTTP {exc.response.status_code}: {exc.response.text[:200]}",
                    retriable=exc.response.status_code in {429, 500, 502, 503, 504},
                ) from exc
            except httpx.HTTPError as exc:
                raise ProviderError(f"ollama transport error: {exc}", retriable=True) from exc
            latency_ms = (time.perf_counter() - start) * 1000

            data: dict[str, Any] = resp.json()
            usage = _to_token_usage(data)
            set_llm_attrs(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=0.0,
                cache_hit=False,
                latency_ms=latency_ms,
            )

        text, tool_uses, stop_reason = _parse_choices(data)
        return CompletionResponse(
            text=text,
            tool_uses=tool_uses,
            stop_reason=stop_reason,
            usage=usage,
            raw=data,
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
        """Constrain output via the OpenAI-compatible `response_format` JSON schema."""
        del cache_system  # Ollama has no prompt caching; accepted for interface parity
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": schema.model_json_schema(),
                    "strict": True,
                },
            },
        }
        with llm_span(model, phase=phase):
            start = time.perf_counter()
            try:
                resp = await self._client.post(f"{self._base_url}/chat/completions", json=payload)
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ProviderError(
                    f"ollama HTTP {exc.response.status_code}: {exc.response.text[:200]}",
                    retriable=exc.response.status_code in {429, 500, 502, 503, 504},
                ) from exc
            latency_ms = (time.perf_counter() - start) * 1000
            data: dict[str, Any] = resp.json()
            usage = _to_token_usage(data)
            set_llm_attrs(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=0.0,
                cache_hit=False,
                latency_ms=latency_ms,
            )

        try:
            content = data["choices"][0]["message"]["content"] or "{}"
            value = schema.model_validate_json(content)
        except (KeyError, json.JSONDecodeError, ValidationError) as exc:
            raise ProviderError(f"structured validation failed: {exc}") from exc
        return value, usage

    def estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


# ============================ helpers ============================


def _build_payload(req: CompletionRequest) -> dict[str, Any]:
    msgs: list[dict[str, Any]] = []
    if req.system:
        msgs.append({"role": "system", "content": req.system})
    for m in req.messages:
        if m.role is MessageRole.USER:
            msgs.append(_user_msg(m))
        elif m.role is MessageRole.ASSISTANT:
            msgs.append(_assistant_msg(m))
        elif m.role is MessageRole.TOOL:
            msgs.extend(
                {"role": "tool", "tool_call_id": tr.tool_use_id, "content": tr.content}
                for tr in m.tool_results
            )
    payload: dict[str, Any] = {
        "model": req.model,
        "messages": msgs,
        "temperature": req.temperature,
        "max_tokens": req.max_tokens,
    }
    if req.tools:
        payload["tools"] = [{"type": "function", "function": _tool_to_openai(t)} for t in req.tools]
        payload["tool_choice"] = (
            "required"
            if req.tool_choice == "any"
            else "auto"
            if req.tool_choice == "auto"
            else "none"
        )
    return payload


def _user_msg(m: Message) -> dict[str, Any]:
    if m.tool_results:
        # Should have been split into TOOL role; fold gracefully anyway.
        parts: list[dict[str, Any]] = []
        if m.content:
            parts.append({"type": "text", "text": m.content})
        return {"role": "user", "content": parts or m.content}
    return {"role": "user", "content": m.content}


def _assistant_msg(m: Message) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": m.content}
    if m.tool_uses:
        msg["tool_calls"] = [
            {
                "id": tu.id,
                "type": "function",
                "function": {"name": tu.name, "arguments": json.dumps(tu.input)},
            }
            for tu in m.tool_uses
        ]
    return msg


def _tool_to_openai(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": t["name"],
        "description": t.get("description", ""),
        "parameters": t.get("input_schema", {}),
    }


def _parse_choices(data: dict[str, Any]) -> tuple[str, tuple[ToolUseBlock, ...], StopReason]:
    choice = data["choices"][0]
    msg = choice.get("message", {})
    text: str = msg.get("content") or ""
    tool_uses: list[ToolUseBlock] = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        tool_uses.append(ToolUseBlock(id=tc.get("id", ""), name=fn.get("name", ""), input=args))
    finish = choice.get("finish_reason") or "stop"
    stop_reason: StopReason = (
        "tool_use"
        if finish == "tool_calls"
        else "end_turn"
        if finish == "stop"
        else "max_tokens"
        if finish == "length"
        else "end_turn"
    )
    return text, tuple(tool_uses), stop_reason


def _to_token_usage(data: dict[str, Any]) -> TokenUsage:
    u = data.get("usage", {}) or {}
    return TokenUsage(
        input_tokens=int(u.get("prompt_tokens", 0) or 0),
        output_tokens=int(u.get("completion_tokens", 0) or 0),
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cost_usd=0.0,
    )

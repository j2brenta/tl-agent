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
import re
import time
from typing import Any, cast

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
        # Native `/api/chat` lives at the server root; strip the OpenAI-compat
        # `/v1` suffix if the caller pointed us at it. structured() uses native
        # because format-constrained JSON only enforces via this endpoint.
        self._native_base = (
            self._base_url[:-3] if self._base_url.endswith("/v1") else self._base_url
        )
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
        """Constrain output via Ollama's native `/api/chat` with `format: <schema>`.

        The OpenAI-compat `response_format: json_schema` shim is loose — many
        builds ignore `strict` and let free text through, then the empty
        content validates to `{}` and the caller sees "field required" errors.
        Native `format` enforces JSON via grammar-constrained sampling.

        We also defensively strip leading `<think>...</think>` blocks (qwen3
        and other reasoning models emit them) and retry once with a stricter
        reminder if the first attempt fails validation.
        """
        del cache_system  # Ollama has no prompt caching; accepted for interface parity

        first_value, usage = await self._structured_once(
            model=model,
            system=system,
            user=user,
            schema=schema,
            max_tokens=max_tokens,
            temperature=temperature,
            phase=phase,
        )
        if first_value is not None:
            return first_value, usage

        # Retry once with an explicit JSON-only reminder. qwen3-style reasoning
        # models occasionally exhaust their budget inside `<think>` and emit
        # empty content; nudging them past that recovers most failures.
        logger.warning(
            "ollama.structured retrying schema=%s phase=%s after empty/invalid response",
            schema.__name__,
            phase,
        )
        retry_system = system + (
            "\n\nIMPORTANT: respond with a single valid JSON object that matches "
            "the requested schema. Do not include any reasoning, prose, "
            "code fences, or <think> blocks — only the JSON."
        )
        retry_user = user + "\n\n/no_think"  # qwen3-specific; harmless to other models
        retry_value, retry_usage = await self._structured_once(
            model=model,
            system=retry_system,
            user=retry_user,
            schema=schema,
            max_tokens=max_tokens,
            temperature=temperature,
            phase=phase,
        )
        merged = TokenUsage(
            input_tokens=usage.input_tokens + retry_usage.input_tokens,
            output_tokens=usage.output_tokens + retry_usage.output_tokens,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            cost_usd=0.0,
        )
        if retry_value is None:
            raise ProviderError(
                f"structured validation failed after retry: schema={schema.__name__}"
            )
        return retry_value, merged

    async def _structured_once[T: BaseModel](
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: type[T],
        max_tokens: int,
        temperature: float,
        phase: str | None,
    ) -> tuple[T | None, TokenUsage]:
        """One call against `/api/chat`; returns None on empty/invalid output."""
        payload: dict[str, Any] = {
            "model": model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "format": schema.model_json_schema(),
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        with llm_span(model, phase=phase):
            start = time.perf_counter()
            try:
                resp = await self._client.post(f"{self._native_base}/api/chat", json=payload)
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
            usage = _to_native_token_usage(data)
            set_llm_attrs(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=0.0,
                cache_hit=False,
                latency_ms=latency_ms,
            )

        raw_content = cast(
            str, (cast(dict[str, Any], data.get("message")) or {}).get("content") or ""
        )
        content = _strip_thinking(raw_content).strip()
        if not content:
            return None, usage
        try:
            value = schema.model_validate_json(content)
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.warning(
                "ollama.structured invalid payload schema=%s phase=%s err=%s preview=%r",
                schema.__name__,
                phase,
                type(exc).__name__,
                content[:200],
            )
            return None, usage
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
    choice = cast(dict[str, Any], data["choices"][0])
    msg = cast(dict[str, Any], choice.get("message", {}))
    text: str = cast(str, msg.get("content") or "")
    tool_uses: list[ToolUseBlock] = []
    for tc in cast(list[Any], msg.get("tool_calls") or []):
        fn = cast(dict[str, Any], tc.get("function", {}))
        try:
            args: dict[str, Any] = json.loads(cast(str, fn.get("arguments") or "{}"))
        except json.JSONDecodeError:
            args = {}
        tool_uses.append(
            ToolUseBlock(
                id=cast(str, tc.get("id", "")), name=cast(str, fn.get("name", "")), input=args
            )
        )
    finish = cast(str, choice.get("finish_reason") or "stop")
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
    u = cast(dict[str, Any], data.get("usage", {}) or {})
    return TokenUsage(
        input_tokens=int(cast(int, u.get("prompt_tokens", 0) or 0)),
        output_tokens=int(cast(int, u.get("completion_tokens", 0) or 0)),
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cost_usd=0.0,
    )


def _to_native_token_usage(data: dict[str, Any]) -> TokenUsage:
    """Token counts from Ollama's native `/api/chat` (different field names)."""
    return TokenUsage(
        input_tokens=int(data.get("prompt_eval_count", 0) or 0),
        output_tokens=int(data.get("eval_count", 0) or 0),
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cost_usd=0.0,
    )


_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


def _strip_thinking(text: str) -> str:
    """Remove `<think>...</think>` reasoning blocks (qwen3, deepseek-r1, etc.).

    Handles a stray opening `<think>` without a close by dropping everything
    after it — better to fall through to a retry than to feed half a reasoning
    trace into the JSON parser.
    """
    cleaned = _THINK_BLOCK_RE.sub("", text)
    if "<think>" in cleaned.lower():
        cleaned = cleaned[: cleaned.lower().index("<think>")]
    return cleaned

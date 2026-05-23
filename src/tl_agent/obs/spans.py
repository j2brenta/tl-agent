"""Span decorators and helpers.

Three pre-made shapes:
  @phase_span("phase2_triage")  — wraps one phase function
  @tool_span("jira.get_ticket") — wraps one tool invocation
  @llm_span("haiku")            — wraps one LLM call

All three are async-aware; they set semantic attributes the JSONL sink + the
Phoenix UI use to render the span tree usefully.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from tl_agent.obs.tracing import tracer


def phase_span[**P, R](
    name: str,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Wrap a phase coroutine in a span named `tl_agent.phase.<name>`."""

    def decorator(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with tracer().start_as_current_span(
                f"tl_agent.phase.{name}", attributes={"tl_agent.layer": "phase"}
            ) as span:
                try:
                    result = await fn(*args, **kwargs)
                except Exception as exc:
                    span.record_exception(exc)
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                    raise
                span.set_status(Status(StatusCode.OK))
                return result

        return wrapper

    return decorator


def tool_span(tool_name: str) -> Any:
    """Context-manager factory used inside the BaseTool wrapper.

    Returns an opentelemetry span context manager pre-populated with
    `tl_agent.layer=tool` and `tl_agent.tool=<name>`.
    """
    return tracer().start_as_current_span(
        f"tl_agent.tool.{tool_name}",
        attributes={"tl_agent.layer": "tool", "tl_agent.tool": tool_name},
    )


def llm_span(model: str, *, phase: str | None = None) -> Any:
    """Context-manager factory used inside the LLM router.

    Caller is responsible for setting `tl_agent.tokens.in/out`, `tl_agent.cost_usd`,
    `tl_agent.cache_hit` etc. via `set_llm_attrs` once the call returns.
    """
    attrs: dict[str, Any] = {"tl_agent.layer": "llm", "tl_agent.model": model}
    if phase:
        attrs["tl_agent.phase"] = phase
    return tracer().start_as_current_span(f"tl_agent.llm.{model}", attributes=attrs)


def set_llm_attrs(
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cost_usd: float | None = None,
    cache_hit: bool | None = None,
    latency_ms: float | None = None,
) -> None:
    """Set canonical LLM-span attributes on the *current* span.

    Call this from inside `with llm_span(...)`.
    """
    span = trace.get_current_span()
    if input_tokens is not None:
        span.set_attribute("tl_agent.tokens.input", input_tokens)
    if output_tokens is not None:
        span.set_attribute("tl_agent.tokens.output", output_tokens)
    if cache_read_tokens is not None:
        span.set_attribute("tl_agent.tokens.cache_read", cache_read_tokens)
    if cost_usd is not None:
        span.set_attribute("tl_agent.cost_usd", cost_usd)
    if cache_hit is not None:
        span.set_attribute("tl_agent.cache_hit", cache_hit)
    if latency_ms is not None:
        span.set_attribute("tl_agent.latency_ms", latency_ms)

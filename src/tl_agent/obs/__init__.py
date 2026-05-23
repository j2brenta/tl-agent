"""Observability — OpenTelemetry tracing + JSONL sink + per-run metrics."""

from tl_agent.obs.metrics import RunMetrics
from tl_agent.obs.spans import llm_span, phase_span, set_llm_attrs, tool_span
from tl_agent.obs.tracing import init_tracing, tracer

__all__ = [
    "RunMetrics",
    "init_tracing",
    "llm_span",
    "phase_span",
    "set_llm_attrs",
    "tool_span",
    "tracer",
]

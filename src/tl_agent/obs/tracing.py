"""OpenTelemetry setup.

Two sinks per span:
  1. OTLP/HTTP → Phoenix (run in docker-compose; UI on :6006).
  2. JSONL → traces/YYYY-MM-DD/<run_id>.jsonl (git-diffable; grep-friendly).

Phoenix gives you the span-tree UI ("reconstruct a bad run from the log");
the JSONL sink gives you reviewable diffs in PRs and a path to replay.

Configure once per process via `init_tracing()`. Idempotent.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from tl_agent.obs.jsonl_sink import JsonlSpanProcessor
from tl_agent.settings import get_settings

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer

logger = logging.getLogger(__name__)

_initialised = False
_TRACER_NAME = "tl_agent"


def init_tracing(*, service_name: str = "tl-agent", force: bool = False) -> None:
    """Set up the global tracer provider.

    Args:
        service_name: emitted as the OTel `service.name` resource attribute.
        force:        re-initialise even if already done (used by tests).
    """
    global _initialised
    if _initialised and not force:
        return

    settings = get_settings()

    resource = Resource.create({"service.name": service_name, "service.version": "0.1.0"})
    provider = TracerProvider(resource=resource)

    # Phoenix / OTLP export (best effort — never fatal if endpoint is down)
    try:
        otlp_exporter = OTLPSpanExporter(
            endpoint=settings.otlp_endpoint,
            timeout=settings.otlp_timeout_seconds,
        )
        provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
    except Exception as exc:
        logger.warning("otlp exporter init failed: %s — continuing without remote tracing", exc)

    # JSONL mirror (always on unless disabled in settings)
    if settings.enable_jsonl_sink:
        provider.add_span_processor(JsonlSpanProcessor(base_dir=settings.traces_dir))

    trace.set_tracer_provider(provider)

    # Optional: full LLM request/response capture via OpenInference. Off by
    # default because payloads can contain ticket bodies. Turned on via
    # TLA_LOG_LLM_PAYLOADS=1; Phoenix then renders a Conversation tab.
    if settings.log_llm_payloads:
        _instrument_anthropic_payloads()

    _initialised = True
    logger.info(
        "tracing initialised",
        extra={
            "service": service_name,
            "otlp": settings.otlp_endpoint,
            "log_llm_payloads": settings.log_llm_payloads,
        },
    )


def _instrument_anthropic_payloads() -> None:
    """Idempotently enable OpenInference auto-instrumentation for the Anthropic SDK."""
    try:
        from openinference.instrumentation.anthropic import AnthropicInstrumentor
    except ImportError:
        logger.warning(
            "log_llm_payloads=true but openinference-instrumentation-anthropic "
            "is not installed; LLM payloads will not be recorded"
        )
        return
    instrumentor = AnthropicInstrumentor()
    if not instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.instrument()


def tracer() -> Tracer:
    """Get a tracer instance — call after `init_tracing()`.

    Safe to call before init; you'll just get the default (no-op) provider's
    tracer, which silently drops spans. That's the right default for tests.
    """
    return trace.get_tracer(_TRACER_NAME)

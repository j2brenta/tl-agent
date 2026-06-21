"""Observability tests — round-trip spans through JSONL and aggregate metrics."""

from __future__ import annotations

import json
from pathlib import Path

import opentelemetry.trace as ot_trace
import pytest
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from tl_agent.obs import RunMetrics, llm_span, phase_span, set_llm_attrs, tool_span
from tl_agent.obs.jsonl_sink import JsonlSpanExporter
from tl_agent.obs.spans import set_llm_outcome


@pytest.fixture(autouse=True)
def _tracer_provider(tmp_path: Path) -> Path:
    """Install a fresh JSONL-backed provider per test.

    OTel guards set_tracer_provider with a `_TRACER_PROVIDER_SET_ONCE` flag.
    Tests need to reset it so each gets its own exporter (and its own tmp dir).
    """
    ot_trace._TRACER_PROVIDER_SET_ONCE = ot_trace.Once()  # type: ignore[attr-defined]
    ot_trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]
    provider = TracerProvider(resource=Resource.create({"service.name": "test"}))
    provider.add_span_processor(SimpleSpanProcessor(JsonlSpanExporter(tmp_path)))
    trace.set_tracer_provider(provider)
    return tmp_path


async def test_phase_span_records_success() -> None:
    @phase_span("phase2_triage")
    async def work() -> str:
        return "ok"

    result = await work()
    assert result == "ok"


async def test_phase_span_records_exception() -> None:
    @phase_span("phase5_deepdive")
    async def boom() -> None:
        raise RuntimeError("oops")

    with pytest.raises(RuntimeError):
        await boom()


def test_tool_and_llm_spans_emit_attributes(_tracer_provider: Path) -> None:
    with tool_span("jira.get_ticket"):
        pass
    with llm_span("sonnet-4-6", phase="phase3"):
        set_llm_attrs(
            input_tokens=120,
            output_tokens=40,
            cache_read_tokens=80,
            cost_usd=0.003,
            cache_hit=False,
            latency_ms=412.7,
        )

    # Find today's spans file
    today_dirs = list(_tracer_provider.iterdir())
    assert len(today_dirs) == 1
    lines = (today_dirs[0] / "spans.jsonl").read_text(encoding="utf-8").splitlines()
    spans = [json.loads(line) for line in lines]
    by_name = {s["name"]: s for s in spans}

    tool = by_name["tl_agent.tool.jira.get_ticket"]
    assert tool["attributes"]["tl_agent.layer"] == "tool"
    assert tool["attributes"]["tl_agent.tool"] == "jira.get_ticket"

    llm = by_name["tl_agent.llm.sonnet-4-6"]
    assert llm["attributes"]["tl_agent.tokens.input"] == 120
    assert llm["attributes"]["tl_agent.tokens.output"] == 40
    assert llm["attributes"]["tl_agent.cost_usd"] == pytest.approx(0.003)
    assert llm["attributes"]["tl_agent.phase"] == "phase3"


def test_llm_outcome_failure_marks_span_error_with_event(_tracer_provider: Path) -> None:
    with llm_span("qwen3:8b", phase="standup_segments"):
        set_llm_outcome(
            "empty",
            attempt=2,
            detail="no content after stripping <think> blocks",
            preview="<think>reasoning that never ended",
        )

    today_dirs = list(_tracer_provider.iterdir())
    spans = [
        json.loads(line)
        for line in (today_dirs[0] / "spans.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    llm = next(s for s in spans if s["name"] == "tl_agent.llm.qwen3:8b")

    assert llm["status"] == "ERROR"
    assert llm["attributes"]["tl_agent.llm.outcome"] == "empty"
    assert llm["attributes"]["tl_agent.llm.attempt"] == 2
    assert "<think>" in llm["attributes"]["tl_agent.llm.content_preview"]

    event = next(ev for ev in llm["events"] if ev["name"] == "llm.parse_failed")
    assert event["attributes"]["outcome"] == "empty"


def test_llm_outcome_ok_leaves_span_unset(_tracer_provider: Path) -> None:
    with llm_span("qwen3:8b", phase="standup_segments"):
        set_llm_outcome("ok", attempt=1)

    today_dirs = list(_tracer_provider.iterdir())
    spans = [
        json.loads(line)
        for line in (today_dirs[0] / "spans.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    llm = next(s for s in spans if s["name"] == "tl_agent.llm.qwen3:8b")

    assert llm["status"] != "ERROR"
    assert llm["attributes"]["tl_agent.llm.outcome"] == "ok"
    assert not [ev for ev in llm["events"] if ev["name"] == "llm.parse_failed"]


def test_run_metrics_aggregates_from_jsonl(_tracer_provider: Path) -> None:
    # Emit a small fake run
    with llm_span("haiku-4-5"):
        set_llm_attrs(input_tokens=100, output_tokens=20, cost_usd=0.001, cache_hit=False)
    with llm_span("haiku-4-5"):
        set_llm_attrs(input_tokens=80, output_tokens=15, cost_usd=0.0008, cache_hit=True)
    with tool_span("jira.list_sprint"):
        pass

    # Wait for span flush — SimpleSpanProcessor is synchronous so already flushed
    today_dirs = list(_tracer_provider.iterdir())
    assert len(today_dirs) == 1
    jsonl_path = today_dirs[0] / "spans.jsonl"

    metrics = RunMetrics.from_jsonl(jsonl_path)
    assert metrics.llm_calls == 2
    assert metrics.llm_cache_hits == 1
    assert metrics.input_tokens == 180
    assert metrics.output_tokens == 35
    assert metrics.cost_usd == pytest.approx(0.0018)
    assert metrics.cache_hit_rate == pytest.approx(0.5)
    assert metrics.tool_calls == 1

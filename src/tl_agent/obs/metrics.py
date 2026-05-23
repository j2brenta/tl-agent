"""Per-run metrics aggregation.

We do not stand up a separate metrics pipeline (Prometheus, etc.). Per-run
accounting is enough for the kind of analysis we care about: did this run
fit in the token budget, what was the cache hit rate, which phase dominated
latency. Numbers are computed from the JSONL trace by `RunMetrics.from_jsonl`.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RunMetrics:
    """Aggregate counters for one run.

    Built post-hoc by reading the JSONL trace — this means even if the
    in-process counters drift, the source of truth is the durable trace.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    llm_calls: int = 0
    llm_cache_hits: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    latency_ms_by_phase: dict[str, float] = field(default_factory=dict[str, float])

    @property
    def cache_hit_rate(self) -> float:
        if self.llm_calls == 0:
            return 0.0
        return self.llm_cache_hits / self.llm_calls

    @classmethod
    def from_jsonl(cls, path: Path) -> RunMetrics:
        """Replay a JSONL trace file into aggregate metrics."""
        m = cls()
        phase_latency: dict[str, float] = defaultdict(float)
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            span = json.loads(line)
            layer = span.get("attributes", {}).get("tl_agent.layer")
            if layer == "llm":
                m.llm_calls += 1
                attrs = span.get("attributes", {})
                m.input_tokens += int(attrs.get("tl_agent.tokens.input", 0) or 0)
                m.output_tokens += int(attrs.get("tl_agent.tokens.output", 0) or 0)
                m.cache_read_tokens += int(attrs.get("tl_agent.tokens.cache_read", 0) or 0)
                m.cost_usd += float(attrs.get("tl_agent.cost_usd", 0.0) or 0.0)
                if attrs.get("tl_agent.cache_hit"):
                    m.llm_cache_hits += 1
            elif layer == "tool":
                m.tool_calls += 1
                if span.get("status") == "ERROR":
                    m.tool_errors += 1
            elif layer == "phase":
                phase = span["name"].removeprefix("tl_agent.phase.")
                phase_latency[phase] += float(span.get("duration_ms") or 0.0)
        m.latency_ms_by_phase = dict(phase_latency)
        return m

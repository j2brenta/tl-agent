"""JSONL span sink — every span is also written to a daily file on disk.

Why: Phoenix is great for browsing live, but you can't grep a UI, you can't
diff it in a PR, and CI artifacts need a file. The JSONL sink gives you the
same data in `traces/YYYY-MM-DD/<run_id>.jsonl` so:

- `grep tool.failed traces/2026-05-22/*.jsonl` finds the bad run
- a PR that changes prompts can include the eval-trace diff
- `evals/replay.py` reads JSONL to reconstruct an LLM call without Phoenix

Format: one JSON object per line, conforming to OTel-ish keys but flattened
for greppability.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)


class JsonlSpanExporter(SpanExporter):
    """One JSONL file per UTC date, append-only.

    Trace ID partitioning could be smarter (one file per run_id) but UTC date
    is the join key for most queries ("what happened on 2026-05-22") so it
    keeps the file path predictable.
    """

    def __init__(self, base_dir: Path | str) -> None:
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if not spans:
            return SpanExportResult.SUCCESS
        # Group by date so we open each file at most once per batch
        by_date: dict[str, list[str]] = {}
        for span in spans:
            iso = _span_date_iso(span)
            by_date.setdefault(iso, []).append(_span_to_json(span))
        with self._lock:
            for date_iso, lines in by_date.items():
                target = self._base_dir / date_iso
                target.mkdir(parents=True, exist_ok=True)
                trace_file = target / "spans.jsonl"
                with trace_file.open("a", encoding="utf-8") as fh:
                    for line in lines:
                        fh.write(line + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None: ...

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def JsonlSpanProcessor(base_dir: Path | str) -> SpanProcessor:
    """Convenience constructor — bundles the exporter into a SimpleSpanProcessor.

    Synchronous on purpose: JSONL writes are tiny and we want them present
    before a process exits unexpectedly.
    """
    return SimpleSpanProcessor(JsonlSpanExporter(base_dir))


def _span_date_iso(span: ReadableSpan) -> str:
    """Pick the date the span belongs to (UTC, of its start)."""
    if span.start_time is None:
        return datetime.now(UTC).strftime("%Y-%m-%d")
    return datetime.fromtimestamp(span.start_time / 1e9, tz=UTC).strftime("%Y-%m-%d")


def _span_to_json(span: ReadableSpan) -> str:
    """Flatten an OTel ReadableSpan into one JSONL line."""
    ctx = span.get_span_context()
    parent_id = format(span.parent.span_id, "016x") if span.parent else None
    attrs: dict[str, object] = dict(span.attributes or {})
    status = span.status.status_code.name if span.status else "UNSET"
    trace_id = format(ctx.trace_id, "032x") if ctx else None
    span_id = format(ctx.span_id, "016x") if ctx else None
    payload = {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_id": parent_id,
        "name": span.name,
        "kind": span.kind.name,
        "status": status,
        "start_ns": span.start_time,
        "end_ns": span.end_time,
        "duration_ms": (
            (span.end_time - span.start_time) / 1e6 if span.end_time and span.start_time else None
        ),
        "attributes": _sanitize(attrs),
        "events": [
            {
                "name": ev.name,
                "ts_ns": ev.timestamp,
                "attributes": _sanitize(dict(ev.attributes or {})),
            }
            for ev in (span.events or [])
        ],
    }
    return json.dumps(payload, default=str, separators=(",", ":"))


def _sanitize(d: dict[str, object]) -> dict[str, object]:
    """Make span attributes JSON-serialisable; truncate huge strings."""
    out: dict[str, object] = {}
    for k, v in d.items():
        if isinstance(v, str) and len(v) > 4_000:
            out[k] = v[:4_000] + f"…[+{len(v) - 4_000}]"
        else:
            out[k] = v
    return out

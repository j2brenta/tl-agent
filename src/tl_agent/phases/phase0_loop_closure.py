"""Phase 0 — loop closure (deterministic).

Reads yesterday's open flags and predictions and updates `days_hot`.
Lets the rest of the run reason about "what was hot yesterday that's still
hot today" without re-deriving it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from tl_agent.models import Flag, Prediction
from tl_agent.obs.spans import phase_span
from tl_agent.phases._context import RunContext
from tl_agent.storage.repos import flags, predictions

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoopClosureOutput:
    """Phase 0 result. Passes through to later phases."""

    carried_flags: list[Flag]
    due_predictions: list[Prediction]


@phase_span("phase0_loop_closure")
async def run(ctx: RunContext) -> LoopClosureOutput:
    """Load yesterday's open flags + predictions whose resolve_after has passed."""
    carried = flags.list_open_on(ctx.sqlite, ctx.run_date - timedelta(days=1))
    due = predictions.list_due(ctx.sqlite, ctx.run_date)
    logger.info(
        "phase0.loaded",
        extra={"carried_flags": len(carried), "due_predictions": len(due)},
    )
    return LoopClosureOutput(carried_flags=carried, due_predictions=due)

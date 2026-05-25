"""Phase 5 — ReACT deep-dive on yellow/red hot spots.

One AgentLoop per hot spot, running concurrently (each loop's tool calls
are serial internally — that's the reasoning). Verifier wraps each loop;
on RETRY_WITH_HINT the loop iterates one more time, bounded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from tl_agent.agent.concurrency import ConcurrencyGate, fan_out
from tl_agent.agent.loop import AgentLoop, AgentLoopResult
from tl_agent.agent.stop_conditions import StopConditions
from tl_agent.agent.verifier import Verifier
from tl_agent.llm.prompts import load_prompt
from tl_agent.models import Hotspot, TriageStatus
from tl_agent.obs.spans import phase_span
from tl_agent.phases._context import RunContext
from tl_agent.settings import get_settings
from tl_agent.tools.base import BaseTool
from tl_agent.tools.registry import registry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeepDive:
    """One hotspot + its ReACT result. Phase 6/7 consume this."""

    hotspot: Hotspot
    loop_result: AgentLoopResult


@phase_span("phase5_deepdive")
async def run(ctx: RunContext, *, hotspots: list[Hotspot]) -> list[DeepDive]:
    targets = [h for h in hotspots if h.severity in {TriageStatus.YELLOW, TriageStatus.RED}]
    if not targets:
        return []

    prompt = load_prompt("phase5_deepdive")
    provider, route = ctx.router.for_phase("phase5_deepdive")

    # Verifier uses a separate (cheaper) model — sonnet by default.
    verifier_provider, verifier_route = ctx.router.for_phase("verifier")
    verifier = Verifier(verifier_provider, model=verifier_route.model)

    # Read-only tools only — registry enforces this anyway.
    tools: list[BaseTool[Any, Any]] = registry.for_phase5()

    s = get_settings()
    gate = ConcurrencyGate(name=route.provider, max_concurrent=2)

    async def _one(hot: Hotspot) -> DeepDive:
        loop = AgentLoop(
            provider=provider,
            model=route.model,
            tools=tools,
            system=prompt.body,
            stop_conditions=StopConditions(
                max_steps=s.react_max_steps,
                wall_clock_seconds=s.react_wall_clock_seconds,
                token_budget=s.react_token_budget,
            ),
            verifier=verifier,
            idempotency=ctx.idempotency,
            run_date_iso=ctx.run_date_iso,
        )
        result = await loop.run(task=_task_for_hotspot(hot, ctx))
        return DeepDive(hotspot=hot, loop_result=result)

    return await fan_out(targets, worker=_one, gate=gate)


def _task_for_hotspot(h: Hotspot, ctx: RunContext) -> str:
    """Frame the hot spot as a concrete question for the deep-dive agent.

    The "Known IDs" line is load-bearing: without it the model has no way to
    know which channel/project to pass to get_chat_messages / list_commits
    and tends to guess (`team-standup`, `acme/backend`, etc.), which the
    tool-layer allowlist then rejects. Cheaper to just tell it up front.
    """
    eng_part = f"for {', '.join(h.engineer_ids)}" if h.engineer_ids else "team-wide"
    tickets = f" (related: {', '.join(h.related_ticket_ids)})" if h.related_ticket_ids else ""
    return (
        f"Investigate this hot spot {eng_part}: {h.summary}{tickets}. "
        f"Severity={h.severity.value}, days_hot={h.days_hot}. "
        f"Known IDs (use these verbatim — do not guess): "
        f"chat channel_id={ctx.standup_channel_id!r}, gitlab project={ctx.project!r}. "
        "Use the tools to confirm or refute the framing, then write a 2-3 sentence "
        "diagnosis. Cite every claim."
    )

"""Phase 3 — cross-correlate (one LLM call over the per-engineer outputs)."""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict, Field

from tl_agent.llm.prompts import load_prompt
from tl_agent.models import DailySignals, EngineerTriage, Hotspot
from tl_agent.obs.spans import phase_span
from tl_agent.phases._context import RunContext

logger = logging.getLogger(__name__)


class _HotspotList(BaseModel):
    """Structured-output wrapper — list-at-the-root isn't supported, so we wrap."""

    model_config = ConfigDict(extra="forbid")

    hotspots: list[Hotspot] = Field(default_factory=list[Hotspot])


@phase_span("phase3_correlate")
async def run(
    ctx: RunContext,
    *,
    signals: DailySignals,
    per_engineer: list[EngineerTriage],
) -> list[Hotspot]:
    prompt = load_prompt("phase3_correlate")
    provider, route = ctx.router.for_phase("phase3_correlate")

    user = _build_user(signals, per_engineer)
    try:
        result, usage = await provider.structured(
            model=route.model,
            system=prompt.body,
            user=user,
            schema=_HotspotList,
            max_tokens=route.max_tokens,
            temperature=route.temperature,
            cache_system=route.cache_system,
            phase="phase3_correlate",
        )
    except Exception as exc:
        logger.warning("phase3.failed err_type=%s err=%s", type(exc).__name__, exc)
        return []
    ctx.budget.spend(usage)
    return list(result.hotspots)


def _build_user(signals: DailySignals, per_engineer: list[EngineerTriage]) -> str:
    lines = [f"Sprint day {signals.sprint_day} of {signals.sprint_length_days}", ""]
    lines.append("## Per-engineer triage")
    for triage in per_engineer:
        ev = "; ".join(f"{e.kind.value}:{e.ref}" for e in triage.evidence)
        lines.append(
            f"- {triage.engineer_id}: {triage.status.value} — {triage.one_line_reason} [{ev}]"
        )
    lines.append("")
    lines.append(f"## Sprint tickets ({len(signals.sprint_tickets)})")
    for ticket in signals.sprint_tickets:
        blocks = f" blocks={list(ticket.blocks)}" if ticket.blocks else ""
        blocked = f" blocked_by={list(ticket.blocked_by)}" if ticket.blocked_by else ""
        lines.append(
            f"- {ticket.key} ({ticket.status.value}, {ticket.assignee or '?'}): "
            f"{ticket.summary}{blocks}{blocked}"
        )
    if signals.tickets_added_since_yesterday:
        lines.append("")
        lines.append("## Tickets added since yesterday")
        lines.extend(
            f"- {added_ticket.key} added by {added_ticket.reporter or '?'}: {added_ticket.summary}"
            for added_ticket in signals.tickets_added_since_yesterday
        )
    lines.append("")
    lines.append(f"## Commits ({len(signals.commits)})")
    lines.extend(
        f"- {c.sha[:8]} by {c.author} → {','.join(c.linked_ticket_keys) or '(unlinked)'} "
        f"+{c.insertions}/-{c.deletions}"
        for c in signals.commits[:50]
    )
    return "\n".join(lines)

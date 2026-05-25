"""Phase 2 — per-engineer triage (LLM, parallel).

One Haiku call per engineer. Uses the LLM router's `structured` shape to
constrain the output to `EngineerTriage`. Concurrency is bounded by a
ConcurrencyGate over the provider.
"""

from __future__ import annotations

import logging
from datetime import date

from tl_agent.agent.concurrency import ConcurrencyGate, fan_out
from tl_agent.llm.prompts import load_prompt
from tl_agent.models import DailySignals, EngineerTriage, TriageStatus
from tl_agent.obs.spans import phase_span
from tl_agent.phases._context import RunContext

logger = logging.getLogger(__name__)


@phase_span("phase2_triage")
async def run(ctx: RunContext, signals: DailySignals) -> list[EngineerTriage]:
    """Run per-engineer triage in parallel; one structured LLM call per engineer."""
    prompt = load_prompt("phase2_triage")
    provider, route = ctx.router.for_phase("phase2_triage")
    gate = ConcurrencyGate(name=route.provider, max_concurrent=4)

    async def _one(engineer_id: str) -> EngineerTriage:
        slice_user = _slice_for_engineer(signals, engineer_id)
        try:
            value, usage = await provider.structured(
                model=route.model,
                system=prompt.body,
                user=slice_user,
                schema=EngineerTriage,
                max_tokens=route.max_tokens,
                temperature=route.temperature,
                cache_system=route.cache_system,
                phase="phase2_triage",
            )
        except Exception as exc:
            logger.warning(
                "phase2.engineer_failed engineer=%s err_type=%s err=%s",
                engineer_id,
                type(exc).__name__,
                exc,
            )
            return EngineerTriage(
                engineer_id=engineer_id,
                status=TriageStatus.GREEN,
                one_line_reason="triage failed; defaulted to GREEN",
            )
        ctx.budget.spend(usage)
        # Pin engineer_id even if the model echoed it back differently.
        return value.model_copy(update={"engineer_id": engineer_id})

    engineer_ids = [e.id for e in ctx.team.engineers]
    return await fan_out(engineer_ids, worker=_one, gate=gate)


def _slice_for_engineer(signals: DailySignals, engineer_id: str) -> str:
    """Build the per-engineer user prompt — only their signals.

    Token-efficient: filter sprint tickets to assignee, commits to author.
    """
    today_st = [s for s in signals.standups_today if s.engineer_id == engineer_id]
    yest_st = [s for s in signals.standups_yesterday if s.engineer_id == engineer_id]
    tickets = [t for t in signals.sprint_tickets if t.assignee == engineer_id]
    commits = [c for c in signals.commits if c.author == engineer_id or engineer_id in c.author]

    lines = [
        f"# Engineer: {engineer_id}",
        f"Sprint day: {signals.sprint_day} of {signals.sprint_length_days}",
        "",
        f"## Today's standup ({_iso(today_st[0].date_iso) if today_st else 'none'})",
        today_st[0].raw if today_st else "(no standup today)",
        "",
        f"## Yesterday's standup ({_iso(yest_st[0].date_iso) if yest_st else 'none'})",
        yest_st[0].raw if yest_st else "(no standup yesterday)",
        "",
        f"## Their sprint tickets ({len(tickets)})",
    ]
    lines.extend(
        f"- {t.key} ({t.status.value}, {t.points or '?'} pts, "
        f"updated {t.updated_at.date().isoformat()}): {t.summary}"
        for t in tickets
    )
    lines.append("")
    lines.append(f"## Their commits in the last 24h ({len(commits)})")
    lines.extend(
        f"- {c.sha[:8]} +{c.insertions}/-{c.deletions} in {c.files_changed} files "
        f"→ {','.join(c.linked_ticket_keys) or '(unlinked)'}: {c.message[:80]}"
        for c in commits
    )
    return "\n".join(lines)


def _iso(date_str: str) -> str:
    """Render an ISO date back as a stable label; tolerant of bad input."""
    try:
        return date.fromisoformat(date_str).isoformat()
    except ValueError:
        return date_str

"""Phase 6 — response mode per hot spot (LLM structured)."""

from __future__ import annotations

import logging

from tl_agent.llm.prompts import load_prompt
from tl_agent.models import ResponseDraft, ResponseMode
from tl_agent.obs.spans import phase_span
from tl_agent.phases._context import RunContext
from tl_agent.phases.phase5_deepdive import DeepDive
from tl_agent.storage import load_markdown

logger = logging.getLogger(__name__)


@phase_span("phase6_response_mode")
async def run(ctx: RunContext, *, deep_dives: list[DeepDive]) -> list[ResponseDraft]:
    if not deep_dives:
        return []
    prompt = load_prompt("phase6_response_mode")
    provider, route = ctx.router.for_phase("phase6_response_mode")
    escalation_rules = load_markdown("escalation.md")

    drafts: list[ResponseDraft] = []
    for dd in deep_dives:
        user = _build_user(dd, escalation_rules)
        try:
            draft, usage = await provider.structured(
                model=route.model,
                system=prompt.body,
                user=user,
                schema=ResponseDraft,
                max_tokens=route.max_tokens,
                temperature=route.temperature,
                cache_system=route.cache_system,
                phase="phase6_response_mode",
            )
        except Exception as exc:
            logger.warning("phase6.failed", extra={"err": str(exc), "hotspot": dd.hotspot.id})
            draft = ResponseDraft(
                hotspot_id=dd.hotspot.id,
                mode=ResponseMode.NOTE,
                body="(response-mode selection failed; defaulted to NOTE)",
                rationale="phase 6 fell back",
            )
            drafts.append(draft)
            continue
        ctx.budget.spend(usage)
        # Force hotspot_id even if the model rephrased it.
        drafts.append(draft.model_copy(update={"hotspot_id": dd.hotspot.id}))
    return drafts


def _build_user(dd: DeepDive, escalation_rules: str) -> str:
    h = dd.hotspot
    return (
        f"# Hot spot\n"
        f"id={h.id} type={h.type.value} severity={h.severity.value} "
        f"days_hot={h.days_hot} sprint_critical={h.sprint_critical}\n"
        f"engineer_ids={list(h.engineer_ids)}\n"
        f"related_ticket_ids={list(h.related_ticket_ids)}\n"
        f"summary: {h.summary}\n\n"
        f"# Deep-dive answer\n{dd.loop_result.final_text}\n\n"
        f"# Escalation rules (override defaults)\n{escalation_rules}\n"
    )

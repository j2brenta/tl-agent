"""Phase 7 — compose the per-hotspot artifact bodies."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from tl_agent.llm.prompts import load_prompt
from tl_agent.models import Decision, ResponseDraft, ResponseMode
from tl_agent.obs.spans import phase_span
from tl_agent.phases._context import RunContext
from tl_agent.phases.phase5_deepdive import DeepDive
from tl_agent.storage import load_markdown, transaction
from tl_agent.storage.repos import decisions

logger = logging.getLogger(__name__)


class _ComposedBody(BaseModel):
    """Schema for the composer's structured output."""

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=2000)


@dataclass(frozen=True)
class Brief:
    """The Phase 7 output bundle — what Phase 8 review serves to the TL."""

    decisions: list[Decision]


@phase_span("phase7_compose")
async def run(
    ctx: RunContext,
    *,
    drafts: list[ResponseDraft],
    deep_dives: list[DeepDive],
) -> Brief:
    if not drafts:
        return Brief(decisions=[])
    prompt = load_prompt("phase7_compose")
    provider, route = ctx.router.for_phase("phase7_compose")
    tl_prefs = load_markdown("tl_preferences.md")
    dd_by_id = {dd.hotspot.id: dd for dd in deep_dives}

    decisions_out: list[Decision] = []
    with transaction(ctx.sqlite):
        for draft in drafts:
            dd = dd_by_id.get(draft.hotspot_id)
            body = draft.body or ""  # default to empty if Phase 6 left it
            if dd is not None:
                try:
                    composed, usage = await provider.structured(
                        model=route.model,
                        system=prompt.body,
                        user=_build_user(draft, dd, tl_prefs),
                        schema=_ComposedBody,
                        max_tokens=route.max_tokens,
                        temperature=route.temperature,
                        cache_system=route.cache_system,
                        phase="phase7_compose",
                    )
                    ctx.budget.spend(usage)
                    body = composed.body
                except Exception as exc:
                    logger.warning(
                        "phase7.compose_failed",
                        extra={"hotspot": draft.hotspot_id, "err": str(exc)},
                    )
            decision = Decision(
                id=f"d-{uuid.uuid4().hex[:12]}",
                created_at=datetime.now(UTC),
                hotspot_id=draft.hotspot_id,
                proposed_mode=draft.mode,
                proposed_body=body,
                trace_id=ctx.run_id,
            )
            decisions.insert(ctx.sqlite, decision)
            decisions_out.append(decision)
    return Brief(decisions=decisions_out)


def _build_user(draft: ResponseDraft, dd: DeepDive, tl_prefs: str) -> str:
    return (
        f"# Hot spot\n"
        f"summary: {dd.hotspot.summary}\n"
        f"engineer_ids: {list(dd.hotspot.engineer_ids)}\n"
        f"related_ticket_ids: {list(dd.hotspot.related_ticket_ids)}\n\n"
        f"# Chosen mode\n{draft.mode.value} → target={draft.target or '(none)'}\n"
        f"rationale: {draft.rationale}\n\n"
        f"# Deep-dive answer\n{dd.loop_result.final_text}\n\n"
        f"# TL tone preferences\n{tl_prefs}\n\n"
        f"Write the body for a {_format_label(draft.mode)}."
    )


def _format_label(mode: ResponseMode) -> str:
    return {
        ResponseMode.NOTE: "private note (1-2 sentences)",
        ResponseMode.STANDUP: "standup question (one line)",
        ResponseMode.DM: "DM to the engineer",
        ResponseMode.ESCALATE: "manager escalation note (3 bullets)",
    }[mode]

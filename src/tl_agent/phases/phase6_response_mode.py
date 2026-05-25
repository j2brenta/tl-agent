"""Phase 6 — response mode per hot spot (LLM structured).

Failure-handling strategy: every `provider.structured()` call validates the
model output against `ResponseDraft` (required fields, length bounds). Small
models — local 8B or Anthropic Haiku — occasionally drop `rationale`, emit
empty `body`, or run over length. To balance reliability against cost we
escalate at most once:

    1. Try the route from `config/router*.yaml` (typically Sonnet).
    2. On validation failure, retry exactly once with the `deep` route (Opus).
    3. On second failure, accept what came back — build a best-effort draft
       from the deep-dive's `final_text` as body + rationale so the TL still
       sees the hotspot and the model's reasoning. No NOTE-stub black hole.

The escalation cost is bounded: one Opus call per hotspot in the worst case,
zero in the common case.
"""

from __future__ import annotations

import logging

from tl_agent.llm.base import Provider, ProviderError
from tl_agent.llm.prompts import load_prompt
from tl_agent.llm.router import ModelRoute
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
    # Escalation target: prefer an explicit `phase6_escalation` route (Opus
    # in our default + lite configs), then explicit `deep`. We check
    # `router.routes` directly so the lookup doesn't silently fall back to
    # `default_tier` — in single-tier profiles the equality check below
    # would then turn escalation into a no-op anyway, but explicit is clearer.
    deep_route_pair: tuple[Provider, ModelRoute] | None = None
    for key in ("phase6_escalation", "deep"):
        if key in ctx.router.routes:
            deep_route_pair = ctx.router.for_phase(key)
            break
    escalation_rules = load_markdown("escalation.md")

    drafts: list[ResponseDraft] = []
    for dd in deep_dives:
        user = _build_user(dd, escalation_rules)
        draft = await _draft_with_escalation(
            ctx,
            dd,
            user,
            system=prompt.body,
            primary=(provider, route),
            deep=deep_route_pair,
        )
        # Force hotspot_id even if the model rephrased it.
        drafts.append(draft.model_copy(update={"hotspot_id": dd.hotspot.id}))
    return drafts


async def _draft_with_escalation(
    ctx: RunContext,
    dd: DeepDive,
    user: str,
    *,
    system: str,
    primary: tuple[Provider, ModelRoute],
    deep: tuple[Provider, ModelRoute] | None,
) -> ResponseDraft:
    """Try primary → deep (Opus) → graceful salvage. At most one Opus call."""
    provider, route = primary

    try:
        draft, usage = await provider.structured(
            model=route.model,
            system=system,
            user=user,
            schema=ResponseDraft,
            max_tokens=route.max_tokens,
            temperature=route.temperature,
            cache_system=route.cache_system,
            phase="phase6_response_mode",
        )
        ctx.budget.spend(usage)
        return draft
    except ProviderError as exc:
        logger.warning(
            "phase6.primary_failed hotspot=%s severity=%s model=%s err=%s",
            dd.hotspot.id,
            dd.hotspot.severity.value,
            route.model,
            _short(str(exc)),
        )

    # Escalation: one shot at the deep-tier model if it differs from primary.
    if deep is not None:
        deep_provider, deep_route = deep
        if deep_route.model != route.model or deep_route.provider != route.provider:
            try:
                draft, usage = await deep_provider.structured(
                    model=deep_route.model,
                    system=system,
                    user=user,
                    schema=ResponseDraft,
                    max_tokens=deep_route.max_tokens,
                    temperature=deep_route.temperature,
                    cache_system=deep_route.cache_system,
                    phase="phase6_response_mode",
                )
                ctx.budget.spend(usage)
                logger.info(
                    "phase6.escalated_recovered hotspot=%s model=%s",
                    dd.hotspot.id,
                    deep_route.model,
                )
                return draft
            except ProviderError as exc:
                logger.warning(
                    "phase6.deep_failed hotspot=%s model=%s err=%s",
                    dd.hotspot.id,
                    deep_route.model,
                    _short(str(exc)),
                )

    # Both attempts failed — salvage what we have and flag for HITL. The TL
    # sees a draft with `needs_review=True` instead of a dropped hotspot;
    # the model's deep-dive reasoning is preserved so the TL can finish
    # the call instead of re-deriving context from scratch.
    salvaged_body, salvaged_rationale = _salvage(dd)
    logger.warning(
        "phase6.needs_review hotspot=%s severity=%s body_chars=%d",
        dd.hotspot.id,
        dd.hotspot.severity.value,
        len(salvaged_body),
    )
    return ResponseDraft(
        hotspot_id=dd.hotspot.id,
        mode=ResponseMode.NOTE,
        body=salvaged_body,
        rationale=salvaged_rationale,
        needs_review=True,
    )


def _salvage(dd: DeepDive) -> tuple[str, str]:
    """Build a non-empty (body, rationale) pair from the deep-dive output."""
    final_text = (dd.loop_result.final_text or "").strip()
    # Body: prefix tells the TL at-a-glance this needs hands-on work; the
    # web UI also styles `needs_review=True` cards.
    fallback_body = final_text or f"hot spot {dd.hotspot.id}: {dd.hotspot.summary}"
    body = _truncate(f"[NEEDS REVIEW] {fallback_body}", 2000)
    rationale = _truncate(
        "Phase 6 couldn't extract a clean draft (model omitted/malformed fields "
        f"on both primary and Opus retry). Deep-dive reasoning: "
        f"{final_text or '(no text)'}",
        1500,
    )
    return body, rationale


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def _short(msg: str, limit: int = 300) -> str:
    """Single-line, truncated error string for logs."""
    flat = msg.replace("\n", " ")
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


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

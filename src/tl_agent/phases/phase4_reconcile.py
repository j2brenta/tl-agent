"""Phase 4 — reconcile with yesterday (deterministic counter math).

For each new hot spot, check whether the same one was flagged yesterday:
- If yes ⇒ increment `days_hot`.
- If no  ⇒ new flag with `days_hot=1`.

For each yesterday flag NOT present today ⇒ mark resolved.

Identity: we match hot spots to flags by (type, sorted engineer_ids,
sorted related_ticket_ids). Approximate but works for the team-of-four
scope. A more robust approach (embeddings) is overkill here.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import timedelta

from tl_agent.models import Flag, Hotspot
from tl_agent.obs.spans import phase_span
from tl_agent.phases._context import RunContext
from tl_agent.storage import transaction
from tl_agent.storage.repos import flags as flags_repo

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconcileOutput:
    """Phase 4 output: hot spots with their reconciled days_hot."""

    hotspots: list[Hotspot]
    closed_flag_ids: list[str]


@phase_span("phase4_reconcile")
async def run(ctx: RunContext, *, today_hotspots: list[Hotspot]) -> ReconcileOutput:
    yesterday = ctx.run_date - timedelta(days=1)
    open_yesterday = flags_repo.list_open_on(ctx.sqlite, yesterday)
    by_identity: dict[str, Flag] = {_identity(f): f for f in open_yesterday}

    reconciled: list[Hotspot] = []
    seen_identities: set[str] = set()
    with transaction(ctx.sqlite):
        for h in today_hotspots:
            ident = _identity(_flag_from_hotspot(h))
            seen_identities.add(ident)
            prior = by_identity.get(ident)
            days_hot = (prior.days_hot + 1) if prior else 1
            reconciled.append(h.model_copy(update={"days_hot": days_hot}))
            # Persist as today's flag (upsert on id).
            flag = _flag_from_hotspot(
                h, days_hot=days_hot, first_seen=(prior.first_seen if prior else ctx.run_date)
            )
            flags_repo.upsert(ctx.sqlite, flag, run_date=ctx.run_date)

        closed_ids: list[str] = []
        for prior in open_yesterday:
            if _identity(prior) in seen_identities:
                continue
            flags_repo.mark_resolved(
                ctx.sqlite, prior.id, note="not flagged today", resolved_on=ctx.run_date
            )
            closed_ids.append(prior.id)

    logger.info(
        "phase4.reconciled",
        extra={
            "hot": len(reconciled),
            "closed": len(closed_ids),
            "max_days_hot": max((h.days_hot for h in reconciled), default=0),
        },
    )
    return ReconcileOutput(hotspots=reconciled, closed_flag_ids=closed_ids)


def _identity(flag: Flag) -> str:
    """Stable hash of the (type, engineer, related-tickets) tuple."""
    engineer_part = flag.engineer_id or ""
    tickets_part = ",".join(sorted(flag.related_ticket_ids))
    blob = f"{flag.type.value}|{engineer_part}|{tickets_part}"
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _flag_from_hotspot(h: Hotspot, *, days_hot: int = 1, first_seen: object | None = None) -> Flag:
    """Project a Hotspot down to a Flag for persistence + identity matching."""
    from datetime import date as date_cls

    fs = first_seen if isinstance(first_seen, date_cls) else date_cls.today()
    eng = h.engineer_ids[0] if len(h.engineer_ids) == 1 else None
    return Flag(
        id=f"flag-{_identity_input(h)}",
        type=h.type,
        title=h.summary[:120],
        engineer_id=eng,
        related_ticket_ids=h.related_ticket_ids,
        status=h.severity,
        days_hot=days_hot,
        first_seen=fs,
        last_seen=fs,
    )


def _identity_input(h: Hotspot) -> str:
    eng = h.engineer_ids[0] if len(h.engineer_ids) == 1 else "team"
    return f"{h.type.value}-{eng}-{','.join(sorted(h.related_ticket_ids)) or 'none'}"

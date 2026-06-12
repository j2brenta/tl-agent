"""Standup message segmentation — split + classify update vs off-topic.

One Haiku call per *uncached* standup message, splitting `raw` text into
segments and classifying each as `update` (project-related) or `off_topic`
(banter, links, life updates — a team-mood signal for a later phase).

Results are cached in `standup_segments` keyed by `(chat_message_id,
engineer_id, segment_index)`: a message that's already been parsed — whether
that happened via the Workflow "Collect Standup" button, the Sprint page's
"Import from Mattermost", or a prior pipeline run — is never re-sent to the
LLM. This is the reuse contract: clicking "Run now" after "Collect Standup"
costs zero additional LLM calls for messages already parsed.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tl_agent.agent.concurrency import ConcurrencyGate, fan_out
from tl_agent.llm.budget import BudgetTracker
from tl_agent.llm.prompts import load_prompt
from tl_agent.llm.router import Router
from tl_agent.models.signals import StandupMessage, StandupSegment, StandupSegmentKind
from tl_agent.storage.repos import standup_segments as segments_repo

logger = logging.getLogger(__name__)


class _SegmentOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    kind: Literal["update", "off_topic"]


class _SegmentsOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segments: list[_SegmentOut] = Field(min_length=1)


async def parse_segments(
    conn: sqlite3.Connection,
    router: Router,
    messages: list[StandupMessage],
    *,
    notes: list[str],
    budget: BudgetTracker | None = None,
) -> list[StandupSegment]:
    """Segment + classify each message, reusing cached results where possible."""
    prompt = load_prompt("standup_segments")
    provider, route = router.for_phase("standup_segments")
    gate = ConcurrencyGate(name=route.provider, max_concurrent=4)

    async def _one(msg: StandupMessage) -> list[StandupSegment]:
        if msg.chat_message_id:
            cached = segments_repo.get_for_message(
                conn, chat_message_id=msg.chat_message_id, engineer_id=msg.engineer_id
            )
            if cached:
                return cached

        try:
            value, usage = await provider.structured(
                model=route.model,
                system=prompt.body,
                user=msg.raw,
                schema=_SegmentsOut,
                max_tokens=route.max_tokens,
                temperature=route.temperature,
                cache_system=route.cache_system,
                phase="standup_segments",
            )
        except Exception as exc:
            notes.append(
                f"standup_segments: parse failed for {msg.engineer_id} ({type(exc).__name__}); "
                "treated as a single update segment"
            )
            return [
                StandupSegment(
                    engineer_id=msg.engineer_id,
                    date_iso=msg.date_iso,
                    chat_message_id=msg.chat_message_id,
                    chat_channel_id=msg.chat_channel_id,
                    segment_index=0,
                    text=msg.raw,
                    kind=StandupSegmentKind.UPDATE,
                )
            ]

        if budget is not None:
            budget.spend(usage)

        parsed = [
            StandupSegment(
                engineer_id=msg.engineer_id,
                date_iso=msg.date_iso,
                chat_message_id=msg.chat_message_id,
                chat_channel_id=msg.chat_channel_id,
                segment_index=i,
                text=seg.text,
                kind=StandupSegmentKind(seg.kind),
            )
            for i, seg in enumerate(value.segments)
        ]
        if msg.chat_message_id:
            segments_repo.upsert_many(conn, parsed)
        return parsed

    results = await fan_out(messages, worker=_one, gate=gate)
    return [seg for batch in results for seg in batch]

"""Phase 1 — collect (deterministic, parallel).

Fan-out: pull from Jira, GitLab, and the chat provider in parallel via
asyncio.gather. Each branch tolerates upstream failure (logs a warning,
returns empty) so the run can produce a partial brief rather than abort —
the brief itself records which sources were unavailable.

Output is `DailySignals` — the single envelope passed to Phase 2/3.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime, time, timedelta

from tl_agent.models import (
    DailySignals,
    Engineer,
    GitCommit,
    JiraTicket,
    StandupMessage,
)
from tl_agent.obs.spans import phase_span
from tl_agent.phases._context import RunContext
from tl_agent.tools import ToolResult
from tl_agent.tools.chat.factory import get_chat_provider
from tl_agent.tools.gitlab import ListCommitsTool
from tl_agent.tools.jira import ListSprintTool

logger = logging.getLogger(__name__)


@phase_span("phase1_collect")
async def run(ctx: RunContext) -> DailySignals:
    """Run all four fetches in parallel and assemble the envelope."""
    # Window: yesterday 12pm UTC → today 12pm UTC (per plan).
    today = ctx.run_date
    until = datetime.combine(today, time(12, 0), tzinfo=UTC)
    since = until - timedelta(days=1)

    sprint_task = _fetch_sprint(ctx)
    commits_task = _fetch_commits(ctx, since, until)
    standups_today_task = fetch_standups(ctx, since=since, until=until)
    standups_yesterday_task = fetch_standups(ctx, since=since - timedelta(days=1), until=since)

    sprint, commits, st_today, st_yesterday = await asyncio.gather(
        sprint_task, commits_task, standups_today_task, standups_yesterday_task
    )

    sprint_day, sprint_length, sprint_tickets, added_since = sprint
    # Per-source counts; lets a 0 anywhere be diagnosed without re-running.
    logger.info(
        "phase1.collected",
        extra={
            "run_date": ctx.run_date_iso,
            "window_since": since.isoformat(),
            "window_until": until.isoformat(),
            "sprint_id": ctx.sprint_id,
            "sprint_day": sprint_day,
            "sprint_length_days": sprint_length,
            "sprint_tickets": len(sprint_tickets),
            "tickets_added_since_yesterday": len(added_since),
            "commits": len(commits),
            "standups_today": len(st_today),
            "standups_yesterday": len(st_yesterday),
            "project": ctx.project,
            "standup_channel_id": ctx.standup_channel_id,
            "notes": list(ctx.notes),
        },
    )
    return DailySignals(
        run_date=ctx.run_date_iso,
        standups_today=st_today,
        standups_yesterday=st_yesterday,
        sprint_tickets=sprint_tickets,
        tickets_added_since_yesterday=added_since,
        commits=commits,
        sprint_day=sprint_day,
        sprint_length_days=sprint_length,
    )


# ---------- helpers ----------


async def _fetch_sprint(
    ctx: RunContext,
) -> tuple[int, int, list[JiraTicket], list[JiraTicket]]:
    tool = ListSprintTool()
    result = await tool.invoke({"sprint_id": ctx.sprint_id}, run_date_iso=ctx.run_date_iso)
    if not isinstance(result, ToolResult):
        ctx.notes.append(f"phase1: jira sprint fetch failed ({result.kind.value})")
        return 1, 10, [], []
    out = result.value
    added: list[JiraTicket] = []
    if out.added_since:
        added = [t for t in out.tickets if t.created_at >= out.added_since]
    return out.sprint_day, out.sprint_length_days, list(out.tickets), added


async def _fetch_commits(ctx: RunContext, since: datetime, until: datetime) -> list[GitCommit]:
    tool = ListCommitsTool()
    result = await tool.invoke(
        {"project": ctx.project, "since": since.isoformat(), "until": until.isoformat()},
        run_date_iso=ctx.run_date_iso,
    )
    if not isinstance(result, ToolResult):
        ctx.notes.append(f"phase1: gitlab commits fetch failed ({result.kind.value})")
        return []
    return list(result.value.commits)


async def fetch_standups(
    ctx: RunContext, *, since: datetime, until: datetime
) -> list[StandupMessage]:
    """Pull chat history and map messages → StandupMessage by author."""
    try:
        provider = get_chat_provider()
        msgs = await provider.get_messages(
            channel_id=ctx.standup_channel_id, since=since, until=until, limit=200
        )
    except Exception as exc:
        ctx.notes.append(f"phase1: chat get_messages failed: {exc}")
        return []
    out: list[StandupMessage] = []
    for m in msgs:
        engineer = next((e for e in ctx.team.engineers if e.matches(m.user_id)), None)
        if engineer is not None:
            out.append(
                StandupMessage(
                    engineer_id=engineer.id,
                    date_iso=m.created_at.date().isoformat(),
                    raw=m.text,
                    chat_message_id=m.id,
                    chat_channel_id=m.channel_id,
                )
            )
            continue
        # TL-admin (or anyone unrecognised) posted a transcript-style bulk
        # message — split on `<Name>:` headers and emit one StandupMessage per
        # matched engineer. Same chat_message_id so the trace points back to
        # the same post.
        for eid, body in _split_bulk_standup(m.text, ctx.team.engineers):
            out.append(
                StandupMessage(
                    engineer_id=eid,
                    date_iso=m.created_at.date().isoformat(),
                    raw=body,
                    chat_message_id=m.id,
                    chat_channel_id=m.channel_id,
                )
            )
    return out


_HEADER_RE = re.compile(r"^\s*([A-Za-z][\w\-]*)\s*[:\-—]\s*$", re.MULTILINE)


def _split_bulk_standup(text: str, engineers: Iterable[Engineer]) -> list[tuple[str, str]]:
    """Split a bulk transcript into (engineer_id, body) pairs.

    Matches lines like `John:` / `Matt -` / `Alicia —` as section headers.
    A header is accepted only when the name resolves via `Engineer.matches()`;
    other "Word:" lines (e.g. `Status:`) fall into the previous section's body.
    """
    eng_list = list(engineers)  # may be a generator; we iterate per header
    headers: list[tuple[int, int, str]] = []  # (start, end, engineer_id)
    for match in _HEADER_RE.finditer(text):
        name = match.group(1)
        engineer = next((e for e in eng_list if e.matches(name)), None)
        if engineer is None:
            continue
        headers.append((match.start(), match.end(), engineer.id))
    if not headers:
        return []
    out: list[tuple[str, str]] = []
    for i, (_, body_start, eid) in enumerate(headers):
        body_end = headers[i + 1][0] if i + 1 < len(headers) else len(text)
        body = text[body_start:body_end].strip()
        if body:
            out.append((eid, body))
    return out

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
import sqlite3
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from tl_agent.models import (
    CollectionManifest,
    DailySignals,
    Engineer,
    GitCommit,
    JiraTicket,
    ProjectCoverage,
    StandupMessage,
    UnconfiguredAuthor,
)
from tl_agent.obs.spans import phase_span
from tl_agent.phases._context import RunContext
from tl_agent.phases._sprint import sprint_progress
from tl_agent.phases.sprint_select import resolve_board_id
from tl_agent.phases.standup_parse import parse_segments
from tl_agent.storage import TeamConfig
from tl_agent.storage.markdown_loader import load_allowed_gitlab_projects
from tl_agent.tools import ToolResult
from tl_agent.tools.chat.factory import get_chat_provider
from tl_agent.tools.gitlab import ListCommitsTool, ListGroupProjectsTool
from tl_agent.tools.jira import ListSprintTool

logger = logging.getLogger(__name__)


def gitlab_commit_window(run_date: date, team: TeamConfig) -> tuple[datetime, datetime]:
    """Compute the GitLab commit collection window for `run_date`.

    Normally yesterday 12:00 UTC → today 12:00 UTC (same as the standup
    window). On Mondays, when `team.monday_weekend_lookback` is true, the
    window is extended back to Friday 12:00 UTC so commits pushed on Friday
    evening or over the weekend are not silently dropped.
    """
    until = datetime.combine(run_date, time(12, 0), tzinfo=UTC)
    lookback_days = 1
    if team.monday_weekend_lookback and run_date.weekday() == 0:
        lookback_days = 3
    return until - timedelta(days=lookback_days), until


@phase_span("phase1_collect")
async def run(ctx: RunContext) -> DailySignals:
    """Run all four fetches in parallel and assemble the envelope.

    When `ctx.reuse_cached` is set and a cached collection exists for the run
    date, rebuild the envelope from local storage instead of fetching — the
    Workflow "Reuse stored & run" path. Otherwise fetch fresh and persist the
    result so a later reuse (or the Gitlab tab) can read it back.
    """
    if ctx.reuse_cached:
        cached = _load_cached_signals(ctx)
        if cached is not None:
            logger.info(
                "phase1.reused_cache",
                extra={
                    "run_date": ctx.run_date_iso,
                    "sprint_tickets": len(cached.sprint_tickets),
                    "commits": len(cached.commits),
                    "standups_today": len(cached.standups_today),
                },
            )
            return cached
        ctx.notes.append(
            "phase1: reuse requested but nothing cached for this date; collecting fresh"
        )

    # Standup/sprint window: yesterday 12:00 UTC -> today 12:00 UTC — mirrors
    # the real morning-standup cadence. Demo seed data must be created before
    # 12:00 UTC on run_date to land inside this window (GitLab's Commits API
    # stamps committed_date with wall-clock time at creation; see
    # infra/gitlab/apply_commits.py).
    today = ctx.run_date
    until = datetime.combine(today, time(12, 0), tzinfo=UTC)
    since = until - timedelta(days=1)
    # Commits window may be wider (Friday → Monday) on Mondays.
    commit_since, commit_until = gitlab_commit_window(today, ctx.team)

    sprint_task = _fetch_sprint(ctx, since)
    commits_task = _fetch_commits(ctx, commit_since, commit_until)
    standups_today_task = fetch_standups(ctx, since=since, until=until)
    standups_yesterday_task = fetch_standups(ctx, since=since - timedelta(days=1), until=since)

    sprint, commit_result, st_today, st_yesterday = await asyncio.gather(
        sprint_task, commits_task, standups_today_task, standups_yesterday_task
    )
    commits, manifest = commit_result

    # Segment + classify today's standups (update vs off-topic/mood). Cached
    # by (chat_message_id, engineer_id, segment_index) — messages already
    # parsed via the Workflow "Collect Standup" button or the Sprint page's
    # "Import from Mattermost" are reused here at zero extra LLM cost.
    segments = await parse_segments(
        ctx.sqlite, ctx.router, st_today, notes=ctx.notes, budget=ctx.budget
    )

    sprint_day, sprint_length, sprint_tickets, added_since = sprint
    # Per-source counts; lets a 0 anywhere be diagnosed without re-running.
    logger.info(
        "phase1.collected",
        extra={
            "run_date": ctx.run_date_iso,
            "window_since": since.isoformat(),
            "window_until": until.isoformat(),
            "commit_window_since": commit_since.isoformat(),
            "commit_window_until": commit_until.isoformat(),
            "sprint_id": ctx.sprint_id,
            "sprint_day": sprint_day,
            "sprint_length_days": sprint_length,
            "sprint_tickets": len(sprint_tickets),
            "tickets_added_since_yesterday": len(added_since),
            "commits": len(commits),
            "standups_today": len(st_today),
            "standups_yesterday": len(st_yesterday),
            "standup_segments": len(segments),
            "gitlab_groups": list(ctx.team.gitlab_groups),
            "gitlab_projects": [p.project for p in manifest.projects],
            "gitlab_used_fallback": manifest.used_fallback,
            "gitlab_unconfigured_authors": len(manifest.unconfigured_authors),
            "standup_channel_id": ctx.standup_channel_id,
            "notes": list(ctx.notes),
        },
    )
    signals = DailySignals(
        run_date=ctx.run_date_iso,
        standups_today=st_today,
        standups_yesterday=st_yesterday,
        standup_segments=segments,
        sprint_tickets=sprint_tickets,
        tickets_added_since_yesterday=added_since,
        commits=commits,
        sprint_day=sprint_day,
        sprint_length_days=sprint_length,
        collection_manifest=manifest,
    )
    _persist_collection(ctx, signals)
    return signals


# ---------- persistence + reuse ----------


def _persist_collection(ctx: RunContext, signals: DailySignals) -> None:
    """Cache a fresh collection so a later reuse run / the Gitlab tab can read
    it back without re-fetching. Best-effort: a storage hiccup must not abort a
    run that already produced its signals."""
    from tl_agent.storage import transaction
    from tl_agent.storage.repos import collection_state
    from tl_agent.storage.repos import commits as commits_repo
    from tl_agent.storage.repos import observations as obs_repo
    from tl_agent.storage.repos import snapshots as snapshots_repo

    conn = ctx.sqlite
    try:
        with transaction(conn):
            for ticket in signals.sprint_tickets:
                snapshots_repo.upsert(conn, ctx.run_date, ticket)
            commits_repo.replace_for_date(conn, ctx.run_date, signals.commits)
            for msg in [*signals.standups_today, *signals.standups_yesterday]:
                msg_date = date.fromisoformat(msg.date_iso)
                obs_repo.upsert(
                    conn,
                    obs_id=f"{msg.date_iso}:{msg.engineer_id}",
                    run_date=msg_date,
                    engineer_id=msg.engineer_id,
                    raw=msg.raw,
                    summary=None,
                    chat_message_id=msg.chat_message_id,
                )
            collection_state.set_jira(
                conn,
                ctx.run_date,
                sprint_id=ctx.sprint_id,
                sprint_day=signals.sprint_day,
                sprint_length=signals.sprint_length_days,
                tickets_count=len(signals.sprint_tickets),
            )
            collection_state.set_gitlab(
                conn,
                ctx.run_date,
                manifest=signals.collection_manifest or CollectionManifest(),
                commits_count=len(signals.commits),
            )
    except Exception:
        logger.warning("phase1.persist_failed", extra={"run_date": ctx.run_date_iso})


def _load_cached_signals(ctx: RunContext) -> DailySignals | None:
    """Rebuild DailySignals from locally-cached collection for `ctx.run_date`.

    Returns None when no collection has been recorded for the date (the caller
    then falls back to a fresh fetch). Sprint progress + the GitLab manifest come
    from `collection_state`; tickets/commits/standups/segments from their caches.
    """
    from tl_agent.storage.repos import collection_state
    from tl_agent.storage.repos import commits as commits_repo
    from tl_agent.storage.repos import observations as obs_repo
    from tl_agent.storage.repos import snapshots as snapshots_repo
    from tl_agent.storage.repos import standup_segments as segments_repo

    conn = ctx.sqlite
    state = collection_state.get(conn, ctx.run_date)
    if state is None:
        return None

    today = ctx.run_date
    yesterday = today - timedelta(days=1)

    sprint_tickets = snapshots_repo.list_for_date(conn, today)
    yesterday_tickets = snapshots_repo.list_for_date(conn, yesterday)
    yesterday_keys = {t.key for t in yesterday_tickets}
    added_since = [t for t in sprint_tickets if t.key not in yesterday_keys]

    commits = commits_repo.list_for_date(conn, today)
    standups_today = _observations_as_messages(obs_repo.list_for_date(conn, today))
    standups_yesterday = _observations_as_messages(obs_repo.list_for_date(conn, yesterday))
    segments = segments_repo.list_for_date(conn, today.isoformat())

    manifest = state.manifest or CollectionManifest(gitlab_groups=tuple(ctx.team.gitlab_groups))

    return DailySignals(
        run_date=ctx.run_date_iso,
        standups_today=standups_today,
        standups_yesterday=standups_yesterday,
        standup_segments=segments,
        sprint_tickets=sprint_tickets,
        tickets_added_since_yesterday=added_since,
        commits=commits,
        sprint_day=state.sprint_day or 1,
        sprint_length_days=state.sprint_length or 10,
        collection_manifest=manifest,
    )


def _observations_as_messages(rows: Iterable[Any]) -> list[StandupMessage]:
    return [
        StandupMessage(
            engineer_id=o.engineer_id,
            date_iso=o.run_date.isoformat(),
            raw=o.raw,
            chat_message_id=o.chat_message_id,
        )
        for o in rows
    ]


# ---------- helpers ----------


async def _fetch_sprint(
    ctx: RunContext, since: datetime
) -> tuple[int, int, list[JiraTicket], list[JiraTicket]]:
    # When no sprint was auto-selected, fall back to the board sprint_select
    # discovered and cached — not just the config override, which may be unset.
    board_id = ctx.team.board_id
    if ctx.sprint_id is None:
        board_id = await resolve_board_id(
            ctx.sqlite,
            board_id_override=ctx.team.board_id,
            run_date_iso=ctx.run_date_iso,
            notes=ctx.notes,
        )
    tool = ListSprintTool()
    result = await tool.invoke(
        {"sprint_id": ctx.sprint_id, "board_id": board_id},
        run_date_iso=ctx.run_date_iso,
    )
    if not isinstance(result, ToolResult):
        ctx.notes.append(f"phase1: jira sprint fetch failed ({result.kind.value})")
        return 1, 10, [], []
    out = result.value
    sprint_day, sprint_length = sprint_progress(out.start_date, out.end_date, ctx.run_date)
    tickets = [_resolve_people(t, ctx) for t in out.tickets]
    # A ticket "added since yesterday" is one created within the collection
    # window (Jira has no per-issue "added to sprint" timestamp on this API).
    added = [t for t in tickets if t.created_at >= since]
    return sprint_day, sprint_length, tickets, added


def _resolve_people(ticket: JiraTicket, ctx: RunContext) -> JiraTicket:
    """Fold a ticket's raw Jira assignee/reporter onto the team roster.

    The Jira API reports a `displayName`/`accountId`; we replace it with the
    matching member `id` so downstream phases can compare on a single stable
    identifier. Unresolvable handles (someone off-team) are left untouched and
    surface as an identity-mapping gap in the Workflow tab.
    """
    assignee = ctx.team.resolve(ticket.assignee) or ticket.assignee
    reporter = ctx.team.resolve(ticket.reporter) or ticket.reporter
    if assignee == ticket.assignee and reporter == ticket.reporter:
        return ticket
    return ticket.model_copy(update={"assignee": assignee, "reporter": reporter})


async def _discover_projects(
    team: TeamConfig,
    run_date_iso: str,
    notes: list[str],
    conn: sqlite3.Connection | None = None,
) -> tuple[list[str], bool]:
    """Resolve the team's GitLab projects: every project under each configured group.

    Returns `(projects, used_fallback)` — `used_fallback` is True when the list
    came from `config/gitlab_projects.yaml` instead of live group discovery
    (because `team.gitlab_groups` is empty, or every discovery call came back
    empty).

    When `conn` is given and the discovery registry (`gitlab_projects`,
    populated by the startup discovery pass) is non-empty, those persisted paths
    are used directly — no live GitLab call. The registry is the source of truth
    once warmed; live discovery is the cold-start / no-registry fallback.
    """
    if conn is not None:
        from tl_agent.storage.repos import gitlab_projects

        cached = gitlab_projects.active_paths(conn)
        if cached:
            logger.debug("phase1.gitlab_discovery_registry", extra={"projects": cached})
            return cached, False

    if not team.gitlab_groups:
        fallback = sorted(load_allowed_gitlab_projects())
        logger.debug(
            "phase1.gitlab_discovery_skipped",
            extra={"reason": "no gitlab_groups configured", "fallback_projects": fallback},
        )
        return fallback, True

    tool = ListGroupProjectsTool()

    async def _one(group: str) -> list[str]:
        result = await tool.invoke({"group": group}, run_date_iso=run_date_iso)
        if not isinstance(result, ToolResult):
            notes.append(
                f"phase1: gitlab project discovery failed for group {group!r} "
                f"({result.kind.value}): {result.message}"
            )
            logger.debug(
                "phase1.gitlab_discovery_group_failed",
                extra={"group": group, "kind": result.kind.value, "error_detail": result.message},
            )
            return []
        projects = list(result.value.projects)
        logger.debug(
            "phase1.gitlab_discovery_group_ok",
            extra={"group": group, "projects": projects},
        )
        return projects

    results = await asyncio.gather(*(_one(g) for g in team.gitlab_groups))
    projects = sorted({p for batch in results for p in batch})
    if not projects:
        fallback = sorted(load_allowed_gitlab_projects())
        notes.append("phase1: no GitLab projects discovered; falling back to gitlab_projects.yaml")
        logger.debug(
            "phase1.gitlab_discovery_fallback",
            extra={"gitlab_groups": list(team.gitlab_groups), "fallback_projects": fallback},
        )
        return fallback, True
    logger.debug(
        "phase1.gitlab_discovery_resolved",
        extra={"gitlab_groups": list(team.gitlab_groups), "projects": projects},
    )
    return projects, False


async def _fetch_commits(
    ctx: RunContext, since: datetime, until: datetime
) -> tuple[list[GitCommit], CollectionManifest]:
    return await fetch_commits(ctx.team, since, until, ctx.run_date_iso, ctx.notes, conn=ctx.sqlite)


async def fetch_commits(
    team: TeamConfig,
    since: datetime,
    until: datetime,
    run_date_iso: str,
    notes: list[str],
    conn: sqlite3.Connection | None = None,
) -> tuple[list[GitCommit], CollectionManifest]:
    """Pull every commit in each team GitLab project, then attribute by author.

    One **unfiltered** `list_commits` per project (not per engineer), then each
    commit is bucketed with `team.resolve(author)`:

    - resolves to an *engineer* → kept in the returned `commits` (the
      "team commits" set Phase 2/3 reason over — unchanged semantics).
    - resolves to a non-engineer (TL/PM) → counted in coverage but neither
      triaged nor flagged.
    - resolves to nobody → recorded as an `UnconfiguredAuthor` (someone outside
      the configured team pushed to a team repo, or a roster member is missing
      their `email`/`gitlab_username` in config/team.md).

    Projects come from `list_group_projects` for each group in
    `team.gitlab_groups` (config/team.md → Repo scope);
    `config/gitlab_projects.yaml` is the fallback when no group is configured.
    """
    projects, used_fallback = await _discover_projects(team, run_date_iso, notes, conn)
    engineer_ids = {e.id for e in team.engineers}
    tool = ListCommitsTool()

    async def _one(project: str) -> tuple[str, list[GitCommit] | None, str | None]:
        result = await tool.invoke(
            {"project": project, "since": since.isoformat(), "until": until.isoformat()},
            run_date_iso=run_date_iso,
        )
        if not isinstance(result, ToolResult):
            detail = f"{result.kind.value}: {result.message}"
            notes.append(f"phase1: gitlab commits fetch failed for {project} ({detail})")
            logger.debug(
                "phase1.gitlab_commits_failed",
                extra={
                    "project": project,
                    "kind": result.kind.value,
                    "error_detail": result.message,
                },
            )
            return project, None, detail
        commits = list(result.value.commits)
        logger.debug(
            "phase1.gitlab_commits_ok",
            extra={
                "project": project,
                "commits": len(commits),
                "latency_ms": round(result.latency_ms, 1),
            },
        )
        return project, commits, None

    # One concurrent `list_commits` per project against the same GitLab
    # instance. Fewer calls than the old per-(project, engineer) fan-out, which
    # matters under the Rosetta-emulated GitLab CE image (see
    # infra/docker-compose.yml): a wide fan-out queues behind Puma's worker
    # count and can trip the 10s httpx timeout even when each request is fast.
    logger.info(
        "phase1.gitlab_commits_fanout",
        extra={"projects": projects, "concurrent_requests": len(projects)},
    )
    results = await asyncio.gather(*(_one(p) for p in projects))

    commits: list[GitCommit] = []
    coverage: list[ProjectCoverage] = []
    # Aggregate unconfigured commits by (author, project) so the manifest shows
    # one row per off-roster contributor per repo, with a count + sample sha.
    unconfigured: dict[tuple[str, str], list[GitCommit]] = {}
    for project, project_commits, error in results:
        if project_commits is None:
            coverage.append(ProjectCoverage(project=project, searched=False, error=error))
            continue
        coverage.append(
            ProjectCoverage(project=project, searched=True, commit_count=len(project_commits))
        )
        for c in project_commits:
            resolved = team.resolve(c.author)
            if resolved in engineer_ids:
                commits.append(c)
            elif resolved is None:
                unconfigured.setdefault((c.author, project), []).append(c)

    unconfigured_authors = [
        UnconfiguredAuthor(
            author=author, project=project, commit_count=len(cs), sample_sha=cs[0].sha
        )
        for (author, project), cs in sorted(unconfigured.items())
    ]
    manifest = CollectionManifest(
        gitlab_groups=tuple(team.gitlab_groups),
        used_fallback=used_fallback,
        projects=sorted(coverage, key=lambda p: p.project),
        unconfigured_authors=unconfigured_authors,
    )
    return commits, manifest


async def fetch_standups(
    ctx: RunContext, *, since: datetime, until: datetime
) -> list[StandupMessage]:
    """Pull chat history and map messages → StandupMessage by author."""
    return await fetch_standup_messages(ctx.team, ctx.standup_channel_id, since, until, ctx.notes)


async def fetch_standup_messages(
    team: TeamConfig,
    channel_id: str,
    since: datetime,
    until: datetime,
    notes: list[str],
) -> list[StandupMessage]:
    """Pull chat history and map messages → StandupMessage by author.

    Standalone (no `RunContext`) so one-shot web routes — Workflow's
    "Collect Standup" and the Sprint page's "Import from Mattermost" — can
    call it without constructing a full pipeline context.
    """
    try:
        provider = get_chat_provider()
        msgs = await provider.get_messages(
            channel_id=channel_id, since=since, until=until, limit=200
        )
    except Exception as exc:
        notes.append(f"phase1: chat get_messages failed: {exc}")
        return []
    out: list[StandupMessage] = []
    for m in msgs:
        engineer = next((e for e in team.engineers if e.matches(m.user_id)), None)
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
        for eid, body in _split_bulk_standup(m.text, team.engineers):
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

"""Workflow — start a run and watch its steps, with the sprint human-gate.

Routes:
  GET  /workflow                  — full page (milestones for the selected date)
  GET  /workflow/fragment         — HTMX fragment, self-polls while a run is live
  GET  /workflow/run_prompt       — collect-or-reuse panel for "Run now"
  POST /workflow/run              — kick off the pipeline for a date (background);
                                    `reuse` reuses locally-cached collection
  POST /workflow/sprint/resolve   — resolve an `awaiting_sprint` run by picking
                                    a sprint; relaunches the run with that choice
  GET  /workflow/jira_fragment    — cached Jira tickets for the day (page load)
  POST /workflow/collect_jira     — refresh Jira tickets from the service + persist
  GET  /workflow/gitlab_fragment  — cached GitLab commits for the day (page load)
  POST /workflow/collect_gitlab   — refresh GitLab commits from the service + persist
  POST /workflow/collect_standup  — fetch + segment today's standup (cached)
  GET  /workflow/standup_sources  — pick the standup source (form / Mattermost)
  GET  /workflow/standup_form     — manual standup entry (modal body, one box)
  POST /workflow/collect_standup_form — attribute + persist + segment a pasted standup

The milestone view is a friendly projection of the `runs` table
(`notes.phases` + `notes.signals` + `notes.sprint_decision`) — the same data the
buried run-details fragment shows, promoted to a first-class, live tab.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from datetime import date as date_
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from tl_agent.models import GitCommit, JiraTicket
from tl_agent.web import _dates

logger = logging.getLogger(__name__)

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=select_autoescape())


def _conn() -> sqlite3.Connection:
    from tl_agent.settings import get_settings
    from tl_agent.storage import connect

    return connect(get_settings().sqlite_path)


# Ordered pipeline for computing the "currently running" frontier.
_PHASE_ORDER = [
    "sprint_select",
    "phase0_loop_closure",
    "phase1_collect",
    "phase2_triage",
    "phase3_correlate",
    "phase4_reconcile",
    "phase5_deepdive",
    "phase6_response_mode",
    "phase7_compose",
]


def _sprint_name(sd: dict[str, Any]) -> str | None:
    chosen = sd.get("chosen")
    for c in sd.get("candidates", []):
        if c.get("id") == chosen:
            name = c.get("name")
            return str(name) if name is not None else chosen
    return chosen


def _milestones(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Project a run's phase/signal record into friendly milestone rows."""
    done = {p["phase"] for p in run["phases"] if p.get("status") == "ok"}
    status = run["status"]
    sig = run["signals"]
    sd = run["sprint_decision"]

    frontier = None
    if status == "in_progress":
        frontier = next((p for p in _PHASE_ORDER if p not in done), None)

    def phase_status(phase: str) -> str:
        if phase in done:
            return "done"
        if phase == frontier:
            return "running"
        return "pending"

    # Sprint selection has its own state machine (auto / resolved / pending).
    sd_state = sd.get("state")
    if sd_state == "pending":
        sprint_status = "awaiting"
        sprint_detail = "needs your decision"
    elif sd_state in {"auto", "resolved"}:
        sprint_status = (
            "done" if "sprint_select" in done or run["status"] != "in_progress" else "running"
        )
        name = _sprint_name(sd)
        kind = "selected" if sd_state == "auto" else "you picked"
        sprint_detail = f"{kind}: {name}" if name else kind
    else:
        sprint_status = phase_status("sprint_select")
        sprint_detail = ""

    return [
        {"label": "Sprint selected", "icon": "◈", "status": sprint_status, "detail": sprint_detail},
        {
            "label": "Sprint data from Jira",
            "icon": "▤",
            "status": phase_status("phase1_collect"),
            "detail": f"{sig.get('sprint_tickets', 0)} tickets",
        },
        {
            "label": "Commits from GitLab",
            "icon": "⎇",
            "status": phase_status("phase1_collect"),
            "detail": f"{sig.get('commits', 0)} commits",
        },
        {
            "label": "Standup input",
            "icon": "✎",
            "status": phase_status("phase1_collect"),
            "detail": f"{sig.get('standups_today', 0)} standups",
        },
        {
            "label": "Triage & correlate",
            "icon": "◎",
            "status": phase_status("phase3_correlate"),
            "detail": "",
        },
        {
            "label": "Deep dive",
            "icon": "⚲",
            "status": phase_status("phase5_deepdive"),
            "detail": "",
        },
        {
            "label": "Brief composed",
            "icon": "✓",
            "status": phase_status("phase7_compose"),
            "detail": "",
        },
    ]


def _load_runs(conn: sqlite3.Connection, selected: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM runs WHERE run_date = ? ORDER BY started_at DESC",
        (selected,),
    ).fetchall()
    runs: list[dict[str, Any]] = []
    for r in rows:
        notes: dict[str, Any] = {}
        if r["notes"]:
            with contextlib.suppress(Exception):
                notes = json.loads(r["notes"])
        run = {
            "id": r["id"],
            "run_date": r["run_date"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "status": r["status"],
            "phases": notes.get("phases", []),
            "signals": notes.get("signals", {}),
            "sprint_decision": notes.get("sprint_decision", {}),
        }
        run["milestones"] = _milestones(run)
        runs.append(run)
    return runs


def _available_dates(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT run_date FROM runs ORDER BY run_date DESC LIMIT 30"
    ).fetchall()
    return [r["run_date"] for r in rows]


def _fragment(selected: str, *, just_triggered: bool = False) -> HTMLResponse:
    conn = _conn()
    runs = _load_runs(conn, selected)
    polling = just_triggered or any(r["status"] == "in_progress" for r in runs)
    template = _env.get_template("_workflow_fragment.html")
    return HTMLResponse(template.render(runs=runs, selected_date=selected, polling=polling))


def _coerce_date(date: str | None) -> str:
    if date:
        with contextlib.suppress(ValueError):
            return date_.fromisoformat(date).isoformat()
    return date_.today().isoformat()


@router.get("/workflow", response_class=HTMLResponse)
async def workflow(request: Request, date: str | None = None) -> HTMLResponse:
    selected = _dates.resolve_date(request, date)
    conn = _conn()
    template = _env.get_template("workflow.html")
    response = HTMLResponse(
        template.render(selected_date=selected, available_dates=_available_dates(conn))
    )
    _dates.set_date_cookie(response, selected)
    return response


@router.get("/workflow/fragment", response_class=HTMLResponse)
async def workflow_fragment(date: str | None = None) -> HTMLResponse:
    return _fragment(_coerce_date(date))


# Hold strong refs so the GC doesn't cancel in-flight background runs.
_BG_TASKS: set[asyncio.Task[None]] = set()


def _schedule_run(
    run_date: date_, sprint_id: str | None = None, *, reuse_cached: bool = False
) -> None:
    """Fire-and-forget the pipeline on the running event loop."""

    async def _bg() -> None:
        from tl_agent.phases.orchestrator import run as orch_run

        try:
            await orch_run(run_date, sprint_id=sprint_id, reuse_cached=reuse_cached)
        except Exception:
            # Background task: log, never crash the event loop.
            logger.exception("workflow background run failed (date=%s)", run_date.isoformat())

    task = asyncio.create_task(_bg())
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


@router.get("/workflow/run_prompt", response_class=HTMLResponse)
async def workflow_run_prompt(date: str | None = None) -> HTMLResponse:
    """The collect-or-reuse panel: summarizes what's cached for the day."""
    from tl_agent.storage.repos import collection_state

    selected = _coerce_date(date)
    conn = _conn()
    try:
        state = collection_state.get(conn, date_.fromisoformat(selected))
    finally:
        conn.close()
    template = _env.get_template("_workflow_run_prompt.html")
    return HTMLResponse(
        template.render(
            selected_date=selected,
            jira_collected_at=state.jira_collected_at if state else None,
            gitlab_collected_at=state.gitlab_collected_at if state else None,
            tickets_count=(state.tickets_count if state else 0) or 0,
            commits_count=(state.commits_count if state else 0) or 0,
            has_cache=state is not None,
        )
    )


@router.post("/workflow/run", response_class=HTMLResponse)
async def workflow_run(date: str | None = Form(None), reuse: str = Form("false")) -> HTMLResponse:
    selected = _coerce_date(date)
    reuse_cached = reuse.lower() == "true"
    conn = _conn()
    in_flight = conn.execute(
        "SELECT 1 FROM runs WHERE run_date = ? AND status = 'in_progress' LIMIT 1",
        (selected,),
    ).fetchone()
    if in_flight is None:
        _schedule_run(date_.fromisoformat(selected), reuse_cached=reuse_cached)
    return _fragment(selected, just_triggered=True)


@router.post("/workflow/sprint/resolve", response_class=HTMLResponse)
async def workflow_resolve(run_id: str = Form(...), sprint_id: str = Form(...)) -> HTMLResponse:
    conn = _conn()
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return _fragment(date_.today().isoformat())

    selected = row["run_date"]
    # Mark the parked run resolved (with a pointer to the choice) for the audit
    # trail; the actual work runs as a fresh pipeline with the choice applied.
    notes: dict[str, Any] = {}
    if row["notes"]:
        with contextlib.suppress(Exception):
            notes = json.loads(row["notes"])
    sd = notes.setdefault("sprint_decision", {})
    kind = sd.get("kind", "sprint")
    sd["state"] = "resolved"
    sd["chosen"] = sprint_id  # carries the chosen candidate id (sprint or board)
    conn.execute(
        "UPDATE runs SET status = 'resolved', notes = ? WHERE id = ?",
        (json.dumps(notes), run_id),
    )
    if kind == "board":
        # Persist the board so future runs skip discovery; re-run from scratch
        # so sprint discovery proceeds against the now-known board.
        from tl_agent.storage.repos import resolved_config

        resolved_config.set(conn, resolved_config.JIRA_BOARD_KEY, sprint_id)
        conn.commit()
        _schedule_run(date_.fromisoformat(selected), sprint_id=None)
    else:
        conn.commit()
        _schedule_run(date_.fromisoformat(selected), sprint_id=sprint_id)
    return _fragment(selected, just_triggered=True)


# -------------------- raw data collection (Jira + GitLab) --------------------


def _collect_window(selected: str) -> tuple[datetime, datetime]:
    """Collection window: yesterday 12:00 -> today 12:00 UTC, anchored on
    `selected` — matches phase1_collect.run's window (since the previous
    standup), so these one-shot preview buttons show the same commits/standups
    a real pipeline run for `selected` would collect."""
    run_date = date_.fromisoformat(selected)
    until = datetime(run_date.year, run_date.month, run_date.day, 12, 0, tzinfo=UTC)
    return until - timedelta(days=1), until


class _JiraPull:
    """Result of a one-shot Jira sprint pull (+ sprint progress for caching)."""

    def __init__(
        self,
        sprint_id: str | None,
        tickets: list[JiraTicket],
        error: str | None,
        sprint_day: int | None = None,
        sprint_length: int | None = None,
    ) -> None:
        self.sprint_id = sprint_id
        self.tickets = tickets
        self.error = error
        self.sprint_day = sprint_day
        self.sprint_length = sprint_length


async def _collect_jira(selected: str) -> _JiraPull:
    """Pull the active sprint's tickets.

    Runs the same board resolution as the orchestrator pre-flight (config
    override → DB cache → live discovery) so this one-shot pull discovers a
    board instead of asking `list_sprint` to guess from an empty payload.
    """
    from tl_agent.phases._sprint import sprint_progress
    from tl_agent.phases.sprint_select import resolve_board_id
    from tl_agent.storage import load_team
    from tl_agent.tools import ToolResult
    from tl_agent.tools.jira import ListSprintTool

    conn = _conn()
    try:
        board_id = await resolve_board_id(
            conn, board_id_override=load_team().board_id, run_date_iso=selected
        )
    finally:
        conn.close()
    if board_id is None:
        return _JiraPull(
            None, [], "could not resolve a Jira board — set board_id in config/team.md"
        )

    outcome = await ListSprintTool().invoke({"board_id": board_id}, run_date_iso=selected)
    if not isinstance(outcome, ToolResult):
        return _JiraPull(None, [], outcome.message)
    out = outcome.value
    sprint_day, sprint_length = sprint_progress(
        out.start_date, out.end_date, date_.fromisoformat(selected)
    )
    return _JiraPull(out.sprint_id, list(out.tickets), None, sprint_day, sprint_length)


async def _collect_gitlab(
    selected: str, since: datetime, until: datetime
) -> tuple[list[GitCommit], Any, str | None]:
    """Pull each team engineer's commits across every team project.

    Returns (commits, manifest, error)."""
    from tl_agent.phases.phase1_collect import fetch_commits
    from tl_agent.storage import load_team

    notes: list[str] = []
    conn = _conn()
    try:
        commits, manifest = await fetch_commits(
            load_team(), since, until, selected, notes, conn=conn
        )
    finally:
        conn.close()
    return commits, manifest, "; ".join(notes) or None


async def _collect_standup_segments(
    selected: str, since: datetime, until: datetime
) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch + segment today's standup messages, grouped per engineer.

    Returns (groups, error) where each group is
    `{"engineer": Engineer, "segments": list[StandupSegment]}`.
    Segments are cached by `(chat_message_id, engineer_id, segment_index)` —
    reused by this button, the Sprint page's "Import from Mattermost", and
    the main pipeline run alike.
    """
    from tl_agent.llm.router import build_default
    from tl_agent.phases.phase1_collect import fetch_standup_messages
    from tl_agent.phases.standup_parse import parse_segments
    from tl_agent.storage import load_team

    team = load_team()
    notes: list[str] = []
    messages = await fetch_standup_messages(team, "town-square", since, until, notes)

    conn = _conn()
    try:
        segments = await parse_segments(conn, build_default(), messages, notes=notes)
    finally:
        conn.close()

    by_engineer: dict[str, list[Any]] = {}
    for seg in segments:
        by_engineer.setdefault(seg.engineer_id, []).append(seg)

    groups = [
        {"engineer": e, "segments": by_engineer[e.id]}
        for e in team.engineers
        if e.id in by_engineer
    ]
    return groups, "; ".join(notes) or None


async def _persist_manual_standups(
    selected: str, raw_by_engineer: dict[str, str]
) -> tuple[list[dict[str, Any]], str | None]:
    """Persist manually entered standups, then segment + group them.

    The shared funnel for any non-Mattermost source (the form today; the
    messenger-paste and meeting-recording agents later): given canonical
    `engineer_id -> raw text`, it persists raw observations (FTS + the pipeline
    reuse path) *and* classified segments (Sprint view), so a later "Run now →
    reuse" sees these standups identically to Mattermost-collected ones.

    Each entry uses a deterministic `chat_message_id` (`manual:{date}:{eng}`);
    cached segments are dropped first so an edited resubmission re-segments
    instead of returning the stale cached result.
    """
    from datetime import date as _date

    from tl_agent.llm.router import build_default
    from tl_agent.models.signals import StandupMessage
    from tl_agent.phases.standup_parse import parse_segments
    from tl_agent.storage import load_team, transaction
    from tl_agent.storage.repos import observations as observations_repo
    from tl_agent.storage.repos import standup_segments as segments_repo

    team = load_team()
    run_date = _date.fromisoformat(selected)

    messages: list[StandupMessage] = []
    for engineer in team.engineers:
        raw = (raw_by_engineer.get(engineer.id) or "").strip()
        if not raw:
            continue
        messages.append(
            StandupMessage(
                engineer_id=engineer.id,
                date_iso=selected,
                raw=raw,
                chat_message_id=f"manual:{selected}:{engineer.id}",
                chat_channel_id="manual_form",
            )
        )

    if not messages:
        return [], None

    notes: list[str] = []
    conn = _conn()
    try:
        # Persist raw + bust stale segment cache first (one transaction, no
        # network); then segment, which manages its own writes like the
        # Mattermost path and re-runs the LLM because the cache was cleared.
        with transaction(conn):
            for msg in messages:
                assert msg.chat_message_id is not None
                segments_repo.delete_for_message(
                    conn, chat_message_id=msg.chat_message_id, engineer_id=msg.engineer_id
                )
                observations_repo.upsert(
                    conn,
                    obs_id=f"{selected}:{msg.engineer_id}",
                    run_date=run_date,
                    engineer_id=msg.engineer_id,
                    raw=msg.raw,
                    summary=None,
                    chat_message_id=msg.chat_message_id,
                )
        segments = await parse_segments(conn, build_default(), messages, notes=notes)
    finally:
        conn.close()

    by_engineer: dict[str, list[Any]] = {}
    for seg in segments:
        by_engineer.setdefault(seg.engineer_id, []).append(seg)

    groups = [
        {"engineer": e, "segments": by_engineer[e.id]}
        for e in team.engineers
        if e.id in by_engineer
    ]
    return groups, "; ".join(notes) or None


def _ticket_rows(tickets: list[JiraTicket]) -> list[dict[str, Any]]:
    """Annotate each ticket with assignment health for the table.

    `missing` = no assignee at all; `unknown` = an assignee that doesn't resolve
    to anyone on the team (a likely identity-mapping gap, see the Team tab).
    """
    from tl_agent.storage import load_team

    team = load_team()

    rows: list[dict[str, Any]] = []
    for t in sorted(tickets, key=lambda x: x.key):
        assignee = (t.assignee or "").strip() or None
        resolved = team.resolve(assignee)
        rows.append(
            {
                "ticket": t,
                "assignee": assignee,
                "resolved_id": resolved,
                "missing": assignee is None,
                "unknown": assignee is not None and resolved is None,
            }
        )
    return rows


def _render_jira_fragment(
    selected: str,
    tickets: list[JiraTicket],
    sprint_id: str | None,
    jira_err: str | None,
    collected_at: str | None,
) -> HTMLResponse:
    rows = _ticket_rows(tickets)
    template = _env.get_template("_workflow_jira.html")
    return HTMLResponse(
        template.render(
            selected_date=selected,
            sprint_id=sprint_id,
            rows=rows,
            missing_count=sum(1 for r in rows if r["missing"]),
            unknown_count=sum(1 for r in rows if r["unknown"]),
            jira_err=jira_err,
            collected_at=collected_at,
        )
    )


def _render_gitlab_fragment(
    selected: str, commits: list[GitCommit], gitlab_err: str | None
) -> HTMLResponse:
    from tl_agent.phases.phase1_collect import gitlab_commit_window
    from tl_agent.storage import load_team

    since, until = gitlab_commit_window(date_.fromisoformat(selected), load_team())
    template = _env.get_template("_workflow_gitlab.html")
    return HTMLResponse(
        template.render(
            since=since.isoformat(),
            until=until.isoformat(),
            commits=sorted(commits, key=lambda c: c.committed_at, reverse=True),
            gitlab_err=gitlab_err,
        )
    )


@router.get("/workflow/jira_fragment", response_class=HTMLResponse)
async def workflow_jira_fragment(date: str | None = None) -> HTMLResponse:
    """Cached Jira tickets for the day — rendered on page load, no fetch."""
    from tl_agent.storage.repos import collection_state
    from tl_agent.storage.repos import snapshots as snapshots_repo

    selected = _coerce_date(date)
    run_date = date_.fromisoformat(selected)
    conn = _conn()
    try:
        tickets = snapshots_repo.list_for_date(conn, run_date)
        state = collection_state.get(conn, run_date)
    finally:
        conn.close()
    return _render_jira_fragment(
        selected,
        tickets,
        state.sprint_id if state else None,
        None,
        state.jira_collected_at if state else None,
    )


@router.post("/workflow/collect_jira", response_class=HTMLResponse)
async def workflow_collect_jira(date: str | None = Form(None)) -> HTMLResponse:
    """Refresh Jira sprint tickets from the service and persist them locally."""
    from tl_agent.storage import transaction
    from tl_agent.storage.repos import collection_state
    from tl_agent.storage.repos import snapshots as snapshots_repo

    selected = _coerce_date(date)
    run_date = date_.fromisoformat(selected)
    pull = await _collect_jira(selected)

    if pull.error is None:
        conn = _conn()
        try:
            with transaction(conn):
                for ticket in pull.tickets:
                    snapshots_repo.upsert(conn, run_date, ticket)
                collection_state.set_jira(
                    conn,
                    run_date,
                    sprint_id=pull.sprint_id,
                    sprint_day=pull.sprint_day,
                    sprint_length=pull.sprint_length,
                    tickets_count=len(pull.tickets),
                )
        finally:
            conn.close()

    collected_at = datetime.now(UTC).isoformat() if pull.error is None else None
    return _render_jira_fragment(selected, pull.tickets, pull.sprint_id, pull.error, collected_at)


@router.get("/workflow/gitlab_fragment", response_class=HTMLResponse)
async def workflow_gitlab_fragment(date: str | None = None) -> HTMLResponse:
    """Cached GitLab commits for the day — rendered on page load, no fetch."""
    from tl_agent.storage.repos import commits as commits_repo

    selected = _coerce_date(date)
    conn = _conn()
    try:
        commits = commits_repo.list_for_date(conn, date_.fromisoformat(selected))
    finally:
        conn.close()
    return _render_gitlab_fragment(selected, commits, None)


@router.post("/workflow/collect_gitlab", response_class=HTMLResponse)
async def workflow_collect_gitlab(date: str | None = Form(None)) -> HTMLResponse:
    """Refresh GitLab commits from the service and persist them locally."""
    from tl_agent.phases.phase1_collect import gitlab_commit_window
    from tl_agent.storage import load_team
    from tl_agent.storage.repos import collection_state
    from tl_agent.storage.repos import commits as commits_repo

    selected = _coerce_date(date)
    run_date = date_.fromisoformat(selected)
    since, until = gitlab_commit_window(run_date, load_team())

    commits, manifest, gitlab_err = await _collect_gitlab(selected, since, until)

    if gitlab_err is None:
        conn = _conn()
        try:
            commits_repo.replace_for_date(conn, run_date, commits)
            collection_state.set_gitlab(
                conn, run_date, manifest=manifest, commits_count=len(commits)
            )
            conn.commit()
        finally:
            conn.close()

    return _render_gitlab_fragment(selected, commits, gitlab_err)


@router.post("/workflow/collect_standup", response_class=HTMLResponse)
async def workflow_collect_standup(date: str | None = Form(None)) -> HTMLResponse:
    """One-shot pull of today's standup messages, segmented + classified.

    `update` segments are project-related; `off_topic` segments are banter,
    links, life updates — flagged for a future team-mood signal. Results are
    cached by `(chat_message_id, engineer_id, segment_index)`, so a later
    "Run now" for the same date reuses these segments at zero extra LLM cost.
    """
    selected = _coerce_date(date)
    since, until = _collect_window(selected)

    groups, chat_err = await _collect_standup_segments(selected, since, until)

    template = _env.get_template("_workflow_standup.html")
    return HTMLResponse(
        template.render(
            selected_date=selected,
            since=since.isoformat(),
            until=until.isoformat(),
            groups=groups,
            chat_err=chat_err,
        )
    )


@router.get("/workflow/standup_sources", response_class=HTMLResponse)
async def workflow_standup_sources(date: str | None = None) -> HTMLResponse:
    """Pick where today's standup comes from — form, Mattermost, or (soon)
    the messenger-paste / meeting-recording agents."""
    selected = _coerce_date(date)
    template = _env.get_template("_workflow_standup_sources.html")
    return HTMLResponse(template.render(selected_date=selected))


@router.get("/workflow/standup_form", response_class=HTMLResponse)
async def workflow_standup_form(date: str | None = None) -> HTMLResponse:
    """The manual standup entry form (modal body): one box for the whole team."""
    from tl_agent.storage import load_team

    selected = _coerce_date(date)
    engineer_names = ", ".join(e.display_name for e in load_team().engineers)
    template = _env.get_template("_workflow_standup_form.html")
    return HTMLResponse(template.render(selected_date=selected, engineer_names=engineer_names))


@router.post("/workflow/collect_standup_form", response_class=HTMLResponse)
async def workflow_collect_standup_form(
    date: str | None = Form(None), transcript: str = Form("")
) -> HTMLResponse:
    """Persist a pasted team standup, then segment + classify it.

    `transcript` is one blob for the whole team; `Name:` headers attribute each
    section to an engineer (`standup_intake.attribute_pasted_standups`). Feeds
    the same downstream as the Mattermost button, so a later "Run now → reuse"
    consumes these at zero extra LLM cost.
    """
    from tl_agent.phases.standup_intake import attribute_pasted_standups
    from tl_agent.storage import load_team

    selected = _coerce_date(date)
    raw_by_engineer = attribute_pasted_standups(transcript, load_team().engineers)
    groups, err = await _persist_manual_standups(selected, raw_by_engineer)

    if not groups and err is None and transcript.strip():
        err = (
            "Couldn't attribute any lines to a team member — start each person's "
            "section with their name, e.g. `John:`"
        )

    template = _env.get_template("_workflow_standup.html")
    return HTMLResponse(
        template.render(
            selected_date=selected,
            since=None,
            until=None,
            groups=groups,
            chat_err=err,
        )
    )

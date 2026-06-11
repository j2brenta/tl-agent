"""Storage layer tests against in-memory SQLite + real markdown files.

We deliberately don't mock the DB — the schema + FTS triggers are part of
what we're testing.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

import pytest

from tl_agent.models import (
    ApprovalAction,
    Decision,
    Flag,
    FlagType,
    JiraStatus,
    JiraTicket,
    Prediction,
    PredictionOutcome,
    ResponseMode,
    Role,
    TriageStatus,
)
from tl_agent.storage import connect, initialize, load_team, transaction
from tl_agent.storage.repos import baselines, decisions, flags, observations, predictions, snapshots
from tl_agent.storage.repos.baselines import Baseline
from tl_agent.storage.working_context import WorkingContext


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    initialize(conn)
    return conn


def _today() -> date:
    return date(2026, 5, 22)


def _now() -> datetime:
    return datetime(2026, 5, 22, 9, 0, tzinfo=UTC)


# -------------------- flags --------------------


def test_flags_upsert_and_list_open(db: sqlite3.Connection) -> None:
    f = Flag(
        id="flag-1",
        type=FlagType.INDIVIDUAL,
        title="ENG-12 stuck",
        engineer_id="john",
        status=TriageStatus.YELLOW,
        days_hot=2,
        first_seen=date(2026, 5, 20),
        last_seen=_today(),
        related_ticket_ids=("ENG-12",),
    )
    with transaction(db):
        flags.upsert(db, f, run_date=_today())
    fetched = flags.get(db, "flag-1")
    assert fetched == f
    open_flags = flags.list_open_on(db, _today())
    assert [x.id for x in open_flags] == ["flag-1"]

    with transaction(db):
        flags.mark_resolved(db, "flag-1", note="ticket closed", resolved_on=_today())
    assert flags.list_open_on(db, _today()) == []


# -------------------- predictions --------------------


def test_predictions_due(db: sqlite3.Connection) -> None:
    p = Prediction(
        id="p1",
        made_on=date(2026, 5, 21),
        claim="ENG-12 will close",
        resolve_after=date(2026, 5, 22),
    )
    with transaction(db):
        predictions.insert(db, p)
    due = predictions.list_due(db, _today())
    assert [x.id for x in due] == ["p1"]

    with transaction(db):
        predictions.insert(
            db,
            p.model_copy(update={"outcome": PredictionOutcome.CORRECT, "resolved_on": _today()}),
        )
    assert predictions.list_due(db, _today()) == []


# -------------------- standup observations + FTS --------------------


def test_observations_fts_search(db: sqlite3.Connection) -> None:
    with transaction(db):
        observations.upsert(
            db,
            obs_id="o-john-d1",
            run_date=date(2026, 5, 19),
            engineer_id="john",
            raw="working on the publisher retry policy; no blockers",
            summary="publisher retry",
            chat_message_id=None,
        )
        observations.upsert(
            db,
            obs_id="o-matt-d1",
            run_date=date(2026, 5, 19),
            engineer_id="matt",
            raw="looking into auth refresh issue, hoping to wrap today",
            summary="auth refresh",
            chat_message_id=None,
        )
        observations.upsert(
            db,
            obs_id="o-john-d2",
            run_date=date(2026, 5, 20),
            engineer_id="john",
            raw="still on publisher; hit a snag with retry semantics",
            summary="publisher snag",
            chat_message_id=None,
        )

    hits = observations.search(db, query="publisher")
    assert len(hits) == 2
    assert {(h.engineer_id, h.run_date.isoformat()) for h in hits} == {
        ("john", "2026-05-19"),
        ("john", "2026-05-20"),
    }

    matt_only = observations.search(db, query="auth", engineer_id="matt")
    assert len(matt_only) == 1
    assert matt_only[0].engineer_id == "matt"

    recent = observations.search(db, query="publisher", days=2, today=date(2026, 5, 20))
    assert len(recent) == 2


def test_observations_upsert_updates_in_place(db: sqlite3.Connection) -> None:
    with transaction(db):
        observations.upsert(
            db,
            obs_id="o1",
            run_date=_today(),
            engineer_id="alicia",
            raw="initial",
            summary=None,
            chat_message_id=None,
        )
        observations.upsert(
            db,
            obs_id="o1",
            run_date=_today(),
            engineer_id="alicia",
            raw="updated text",
            summary="updated",
            chat_message_id="m-1",
        )
    got = observations.get(db, run_date=_today(), engineer_id="alicia")
    assert got is not None
    assert got.raw == "updated text"
    assert got.summary == "updated"
    # FTS should reflect the update, not return both versions.
    hits = observations.search(db, query="updated")
    assert len(hits) == 1


# -------------------- baselines --------------------


def test_baselines_round_trip(db: sqlite3.Connection) -> None:
    b = Baseline(
        engineer_id="john",
        window="7d",
        metric="standup_line_count_avg",
        value=3.5,
        computed_at=_now(),
    )
    with transaction(db):
        baselines.upsert(db, b)
    got = baselines.get(db, engineer_id="john", window="7d", metric="standup_line_count_avg")
    assert got == b


# -------------------- ticket snapshots --------------------


def test_ticket_snapshots(db: sqlite3.Connection) -> None:
    t = JiraTicket(
        key="ENG-12",
        summary="add retry",
        status=JiraStatus.IN_PROGRESS,
        assignee="john",
        points=3.0,
        created_at=_now(),
        updated_at=_now(),
    )
    with transaction(db):
        snapshots.upsert(db, _today(), t)
        snapshots.upsert(
            db,
            date(2026, 5, 23),
            t.model_copy(update={"status": JiraStatus.IN_REVIEW}),
        )
    today_ticket = snapshots.get_for_date(db, _today(), "ENG-12")
    assert today_ticket is not None
    assert today_ticket.status is JiraStatus.IN_PROGRESS

    history = snapshots.list_status_history(db, "ENG-12", since=date(2026, 5, 22))
    assert history == [
        (date(2026, 5, 22), JiraStatus.IN_PROGRESS),
        (date(2026, 5, 23), JiraStatus.IN_REVIEW),
    ]


# -------------------- decisions --------------------


def test_decisions_pending_and_approve(db: sqlite3.Connection) -> None:
    d = Decision(
        id="d1",
        created_at=_now(),
        run_date=_now().date().isoformat(),
        hotspot_id="h1",
        proposed_mode=ResponseMode.DM,
        proposed_body="hey john, ENG-12 looks stuck — true?",
    )
    with transaction(db):
        decisions.insert(db, d)
    pending = decisions.list_pending(db)
    assert [x.id for x in pending] == ["d1"]

    approved = d.model_copy(
        update={
            "tl_action": ApprovalAction.APPROVED,
            "tl_acted_at": _now(),
            "final_body": d.proposed_body,
            "final_target": "john",
            "sent_message_id": "mm-msg-99",
            "sent_provider": "mattermost",
        }
    )
    with transaction(db):
        decisions.insert(db, approved)
    assert decisions.list_pending(db) == []
    fetched = decisions.get(db, "d1")
    assert fetched is not None
    assert fetched.tl_action is ApprovalAction.APPROVED
    assert fetched.sent_message_id == "mm-msg-99"


# -------------------- markdown loader --------------------


def test_load_team_parses_four_engineers() -> None:
    team = load_team()
    ids = [e.id for e in team.engineers]
    assert ids == ["john", "matt", "alicia", "karen"]
    john = team.by_id("john")
    assert john is not None
    assert john.aliases == ("jdoe", "johnny")
    assert john.gitlab_username == "john"


def test_load_team_parses_sprint_scope() -> None:
    team = load_team()
    # The reserved `## Sprint scope` section populates board/pattern config…
    assert team.board_id == "ENG"
    assert team.sprint_name_pattern == "Eng Sprint .*"
    # …and never leaks in as a phantom roster member.
    assert team.by_id("sprint-scope") is None
    assert all(m.id != "sprint_scope" for m in team.members)


def test_load_team_separates_leadership_from_engineers() -> None:
    team = load_team()
    # Leadership is excluded from the engineers loop the workflow iterates.
    assert all(m.role_kind is Role.ENGINEER for m in team.engineers)
    assert team.team_lead is not None
    assert team.team_lead.role_kind is Role.TEAM_LEAD
    assert team.product_manager is not None
    assert team.product_manager.role_kind is Role.PRODUCT_MANAGER
    # members is the full roster; by_id resolves leadership too.
    assert len(team.members) == len(team.engineers) + 2
    assert team.by_id(team.product_manager.id) is team.product_manager


# -------------------- working context --------------------


def test_working_context_compacts_when_over_budget() -> None:
    ctx = WorkingContext(budget=50, keep_recent=2)
    for _ in range(8):
        ctx.add("assistant", "x" * 200, name=None)
    assert ctx.needs_compaction()
    ctx.compact(lambda turns: f"<summary of {len(turns)} turns>")
    # 1 summary + 2 recent = 3 turns left
    assert len(ctx.turns) == 3
    assert ctx.turns[0].content.startswith("[context-so-far summary]")

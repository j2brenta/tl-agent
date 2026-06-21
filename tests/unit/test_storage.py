"""Storage layer tests against in-memory SQLite + real markdown files.

We deliberately don't mock the DB — the schema + FTS triggers are part of
what we're testing.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

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
    StandupSegment,
    StandupSegmentKind,
    TriageStatus,
)
from tl_agent.storage import connect, initialize, load_team, transaction
from tl_agent.storage.repos import (
    baselines,
    decisions,
    flags,
    observations,
    predictions,
    snapshots,
    standup_segments,
)
from tl_agent.storage.repos.baselines import Baseline
from tl_agent.storage.working_context import WorkingContext


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    initialize(conn)
    return conn


def test_connect_tolerates_locked_db_during_journal_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """connect() doesn't 500 when another connection is mid-write.

    Regression for "database is locked": in the container's rollback-journal
    (TRUNCATE) mode, connect()'s `PRAGMA journal_mode` takes an EXCLUSIVE lock
    that SQLite refuses *immediately* (no busy-wait) when a writer — e.g. a
    background run's per-phase checkpoints — holds the DB. The mode is an
    optimization, so connect() falls back to the default journal instead of
    raising. We hold a write lock and assert connect() still works.
    """
    monkeypatch.setenv("TLA_SQLITE_JOURNAL_MODE", "TRUNCATE")
    db_path = tmp_path / "lock.db"
    initialize(connect(db_path))  # create the file + schema

    holder = connect(db_path)
    holder.execute("BEGIN IMMEDIATE")  # hold a RESERVED write lock for the test

    # Before the fix the journal_mode PRAGMA raised sqlite3.OperationalError here.
    conn = connect(db_path)
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    # The connection is usable for reads even while the writer holds the lock.
    assert conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()

    conn.close()
    holder.execute("COMMIT")
    holder.close()


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


# -------------------- standup segments --------------------


def test_standup_segments_round_trip_and_get_for_message(db: sqlite3.Connection) -> None:
    segs = [
        StandupSegment(
            engineer_id="john",
            date_iso="2026-05-22",
            chat_message_id="m1",
            chat_channel_id="town-square",
            segment_index=0,
            text="Working on ENG-12 today.",
            kind=StandupSegmentKind.UPDATE,
        ),
        StandupSegment(
            engineer_id="john",
            date_iso="2026-05-22",
            chat_message_id="m1",
            chat_channel_id="town-square",
            segment_index=1,
            text="Also, check out this cool article on Rust!",
            kind=StandupSegmentKind.OFF_TOPIC,
        ),
    ]
    with transaction(db):
        standup_segments.upsert_many(db, segs)

    got = standup_segments.get_for_message(db, chat_message_id="m1", engineer_id="john")
    assert got == segs

    # A message no one has parsed yet returns the empty-list cache miss.
    assert standup_segments.get_for_message(db, chat_message_id="m2", engineer_id="john") == []


def test_standup_segments_upsert_updates_in_place(db: sqlite3.Connection) -> None:
    original = StandupSegment(
        engineer_id="matt",
        date_iso="2026-05-22",
        chat_message_id="m9",
        chat_channel_id="town-square",
        segment_index=0,
        text="initial text",
        kind=StandupSegmentKind.UPDATE,
    )
    updated = original.model_copy(
        update={"text": "revised text", "kind": StandupSegmentKind.OFF_TOPIC}
    )

    with transaction(db):
        standup_segments.upsert_many(db, [original])
        standup_segments.upsert_many(db, [updated])

    got = standup_segments.get_for_message(db, chat_message_id="m9", engineer_id="matt")
    assert got == [updated]


def test_standup_segments_delete_for_message_busts_cache(db: sqlite3.Connection) -> None:
    # An edited manual resubmission reuses the deterministic chat_message_id;
    # delete_for_message clears the cache so the next parse re-segments rather
    # than returning the stale (and now wrong-length) cached result.
    msg_id = "manual:2026-05-22:karen"
    first = [
        StandupSegment(
            engineer_id="karen",
            date_iso="2026-05-22",
            chat_message_id=msg_id,
            chat_channel_id="manual_form",
            segment_index=i,
            text=text,
            kind=StandupSegmentKind.UPDATE,
        )
        for i, text in enumerate(["one", "two"])
    ]
    with transaction(db):
        standup_segments.upsert_many(db, first)
    assert (
        len(standup_segments.get_for_message(db, chat_message_id=msg_id, engineer_id="karen")) == 2
    )

    with transaction(db):
        standup_segments.delete_for_message(db, chat_message_id=msg_id, engineer_id="karen")
    assert standup_segments.get_for_message(db, chat_message_id=msg_id, engineer_id="karen") == []

    rewritten = [
        StandupSegment(
            engineer_id="karen",
            date_iso="2026-05-22",
            chat_message_id=msg_id,
            chat_channel_id="manual_form",
            segment_index=0,
            text="just one now",
            kind=StandupSegmentKind.UPDATE,
        )
    ]
    with transaction(db):
        standup_segments.upsert_many(db, rewritten)
    got = standup_segments.get_for_message(db, chat_message_id=msg_id, engineer_id="karen")
    assert [s.text for s in got] == ["just one now"]


def test_standup_segments_list_for_engineer_date(db: sqlite3.Connection) -> None:
    segs = [
        StandupSegment(
            engineer_id="alicia",
            date_iso="2026-05-22",
            chat_message_id="m1",
            chat_channel_id="town-square",
            segment_index=0,
            text="Yesterday I finished ENG-20.",
            kind=StandupSegmentKind.UPDATE,
        ),
        StandupSegment(
            engineer_id="alicia",
            date_iso="2026-05-22",
            chat_message_id="m1",
            chat_channel_id="town-square",
            segment_index=1,
            text="Found a fun blog post about Rust async.",
            kind=StandupSegmentKind.OFF_TOPIC,
        ),
        StandupSegment(
            engineer_id="alicia",
            date_iso="2026-05-21",
            chat_message_id="m0",
            chat_channel_id="town-square",
            segment_index=0,
            text="yesterday's standup",
            kind=StandupSegmentKind.UPDATE,
        ),
    ]
    with transaction(db):
        standup_segments.upsert_many(db, segs)

    today = standup_segments.list_for_engineer_date(db, engineer_id="alicia", date_iso="2026-05-22")
    assert [s.segment_index for s in today] == [0, 1]
    assert today[1].kind is StandupSegmentKind.OFF_TOPIC


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


def test_team_resolve_matches_jira_account_id_and_display_name() -> None:
    team = load_team()
    # A Jira display name folds onto the member id…
    assert team.resolve("John Doe") == "john"
    # …as does the bare jira_account_id / username…
    assert team.resolve("matt") == "matt"
    # …and an alias (Dana's Jira handle differs from her id).
    assert team.resolve("dpark") == "dana"
    # Case-insensitive; unknown handles and None don't resolve.
    assert team.resolve("ALICIA PARK") == "alicia"
    assert team.resolve("someone-else") is None
    assert team.resolve(None) is None


def test_resolved_config_round_trip(db: sqlite3.Connection) -> None:
    from tl_agent.storage.repos import resolved_config

    key = resolved_config.JIRA_BOARD_KEY
    assert resolved_config.get(db, key) is None
    resolved_config.set(db, key, "ENG")
    assert resolved_config.get(db, key) == "ENG"
    # Upsert replaces in place.
    resolved_config.set(db, key, "OPS")
    assert resolved_config.get(db, key) == "OPS"


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


def test_load_team_monday_weekend_lookback_default() -> None:
    # config/team.md currently sets monday_weekend_lookback: true.
    team = load_team()
    assert team.monday_weekend_lookback is True


def test_load_team_monday_weekend_lookback_parses_false(tmp_path: Path) -> None:
    from tl_agent.storage.markdown_loader import load_team as _load

    team_md = (Path(__file__).parents[2] / "config" / "team.md").read_text()
    team_md = team_md.replace(
        "- **monday_weekend_lookback:** true",
        "- **monday_weekend_lookback:** false",
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "team.md").write_text(team_md)
    team = _load(config_dir=config_dir)
    assert team.monday_weekend_lookback is False


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

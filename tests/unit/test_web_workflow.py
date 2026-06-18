"""Workflow tab — milestone projection + the route surface.

`_milestones` is pure and tested directly. The routes are exercised with a
FastAPI TestClient over a temp SQLite DB; the background pipeline launch is
monkeypatched so tests stay deterministic (no network, no event-loop juggling).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tl_agent.models import (
    Engineer,
    GitCommit,
    JiraStatus,
    JiraTicket,
    StandupSegment,
    StandupSegmentKind,
)
from tl_agent.storage import connect, initialize
from tl_agent.web.routes import workflow as wf
from tl_agent.web.routes.workflow import _collect_window, _milestones, _ticket_rows

# -------------------- pure milestone projection --------------------


def _run(
    status: str,
    phases: list[str],
    *,
    signals: dict[str, Any] | None = None,
    sprint_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": "run-x",
        "run_date": "2026-05-22",
        "started_at": "2026-05-22T09:00:00",
        "finished_at": None,
        "status": status,
        "phases": [{"phase": p, "status": "ok", "duration_s": 0.1} for p in phases],
        "signals": signals or {},
        "sprint_decision": sprint_decision or {},
    }


_ALL_PHASES = [
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


def _by_label(ms: list[dict[str, Any]], label: str) -> dict[str, Any]:
    return next(m for m in ms if m["label"] == label)


def test_milestones_completed_run_all_done() -> None:
    run = _run(
        "completed",
        _ALL_PHASES,
        signals={"sprint_tickets": 13, "commits": 5, "standups_today": 4},
        sprint_decision={
            "state": "auto",
            "chosen": "S-2026-05",
            "candidates": [{"id": "S-2026-05", "name": "Eng Sprint 19"}],
        },
    )
    ms = _milestones(run)
    assert all(m["status"] == "done" for m in ms)
    assert _by_label(ms, "Sprint data from Jira")["detail"] == "13 tickets"
    assert "Eng Sprint 19" in _by_label(ms, "Sprint selected")["detail"]


def test_milestones_awaiting_marks_sprint_decision() -> None:
    run = _run(
        "awaiting_sprint",
        ["sprint_select"],
        sprint_decision={
            "state": "pending",
            "reason": "2 active sprints match",
            "candidates": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}],
        },
    )
    ms = _milestones(run)
    sprint = _by_label(ms, "Sprint selected")
    assert sprint["status"] == "awaiting"
    # Downstream milestones stay pending.
    assert _by_label(ms, "Deep dive")["status"] == "pending"


def test_milestones_in_progress_marks_running_frontier() -> None:
    run = _run(
        "in_progress",
        ["sprint_select", "phase0_loop_closure"],
        sprint_decision={"state": "auto", "chosen": "S-2026-05", "candidates": []},
    )
    ms = _milestones(run)
    # phase1_collect is the frontier → its milestones show running.
    assert _by_label(ms, "Sprint data from Jira")["status"] == "running"
    assert _by_label(ms, "Brief composed")["status"] == "pending"


# -------------------- route surface --------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    db_path = tmp_path / "wf.db"
    monkeypatch.setenv("TLA_SQLITE_PATH", str(db_path))
    conn = connect(db_path)
    initialize(conn)
    conn.close()

    scheduled: list[tuple[str, str | None, bool]] = []

    def _fake_schedule(
        run_date: date, sprint_id: str | None = None, *, reuse_cached: bool = False
    ) -> None:
        scheduled.append((run_date.isoformat(), sprint_id, reuse_cached))

    monkeypatch.setattr(wf, "_schedule_run", _fake_schedule)

    from tl_agent.web.app import app

    client = TestClient(app)
    client.scheduled = scheduled  # type: ignore[attr-defined]
    yield client


def _insert_run(db_path: str, run_id: str, status: str, notes: dict[str, Any]) -> None:
    conn = connect(Path(db_path))
    conn.execute(
        "INSERT INTO runs (id, run_date, started_at, status, notes) VALUES (?,?,?,?,?)",
        (run_id, "2026-05-22", "2026-05-22T09:00:00", status, json.dumps(notes)),
    )
    conn.commit()
    conn.close()


def test_workflow_page_renders(client: TestClient) -> None:
    r = client.get("/workflow?date=2026-05-22")
    assert r.status_code == 200
    assert "Run now" in r.text
    assert 'id="workflow-content"' in r.text


def test_fragment_shows_picker_for_awaiting_run(client: TestClient, tmp_path: Path) -> None:
    _insert_run(
        str(tmp_path / "wf.db"),
        "run-await",
        "awaiting_sprint",
        {
            "phases": [{"phase": "sprint_select", "status": "awaiting", "duration_s": 0.1}],
            "signals": {},
            "sprint_decision": {
                "state": "pending",
                "reason": "2 active sprints match",
                "candidates": [
                    {"id": "S-2026-05", "name": "Eng Sprint 19", "state": "active"},
                    {"id": "S-2026-06", "name": "Eng Sprint 20", "state": "future"},
                ],
            },
        },
    )
    r = client.get("/workflow/fragment?date=2026-05-22")
    assert r.status_code == 200
    assert "Confirm selection" in r.text
    assert "Eng Sprint 19" in r.text
    assert 'value="S-2026-06"' in r.text


def test_run_trigger_schedules_when_idle(client: TestClient) -> None:
    r = client.post("/workflow/run", data={"date": "2026-05-22"})
    assert r.status_code == 200
    assert client.scheduled == [("2026-05-22", None, False)]  # type: ignore[attr-defined]


def test_run_trigger_reuse_passes_flag(client: TestClient) -> None:
    r = client.post("/workflow/run", data={"date": "2026-05-22", "reuse": "true"})
    assert r.status_code == 200
    assert client.scheduled == [("2026-05-22", None, True)]  # type: ignore[attr-defined]


def test_run_trigger_skips_when_in_progress(client: TestClient, tmp_path: Path) -> None:
    _insert_run(
        str(tmp_path / "wf.db"),
        "run-live",
        "in_progress",
        {"phases": [], "signals": {}, "sprint_decision": {}},
    )
    client.post("/workflow/run", data={"date": "2026-05-22"})
    # Already in flight → no new schedule.
    assert client.scheduled == []  # type: ignore[attr-defined]


def test_resolve_marks_resolved_and_reschedules(client: TestClient, tmp_path: Path) -> None:
    db = str(tmp_path / "wf.db")
    _insert_run(
        db,
        "run-await",
        "awaiting_sprint",
        {
            "phases": [],
            "signals": {},
            "sprint_decision": {
                "state": "pending",
                "candidates": [{"id": "S-2026-05", "name": "x"}],
            },
        },
    )
    r = client.post(
        "/workflow/sprint/resolve", data={"run_id": "run-await", "sprint_id": "S-2026-05"}
    )
    assert r.status_code == 200
    assert client.scheduled == [("2026-05-22", "S-2026-05", False)]  # type: ignore[attr-defined]

    conn = connect(Path(db))
    row = conn.execute("SELECT status, notes FROM runs WHERE id = 'run-await'").fetchone()
    conn.close()
    assert row["status"] == "resolved"
    assert json.loads(row["notes"])["sprint_decision"]["chosen"] == "S-2026-05"


# -------------------- data collection (Jira + GitLab) --------------------


def _ticket(key: str, assignee: str | None) -> JiraTicket:
    return JiraTicket(
        key=key,
        summary=f"work on {key}",
        status=JiraStatus.IN_PROGRESS,
        assignee=assignee,
        points=3.0,
        created_at=datetime(2026, 5, 19, 9, 0, tzinfo=UTC),
        updated_at=datetime(2026, 5, 22, 9, 0, tzinfo=UTC),
    )


def test_collect_window_is_yesterday_noon_to_today_noon() -> None:
    since, until = _collect_window("2026-05-22")
    assert until == datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    assert since == datetime(2026, 5, 21, 12, 0, tzinfo=UTC)


def test_ticket_rows_flag_missing_and_unknown() -> None:
    rows = _ticket_rows(
        [_ticket("ENG-1", "john"), _ticket("ENG-2", None), _ticket("ENG-3", "ghost")]
    )
    by_key = {r["ticket"].key: r for r in rows}
    assert by_key["ENG-1"]["resolved_id"] == "john"
    assert by_key["ENG-1"]["missing"] is False and by_key["ENG-1"]["unknown"] is False
    assert by_key["ENG-2"]["missing"] is True
    assert by_key["ENG-3"]["unknown"] is True and by_key["ENG-3"]["assignee"] == "ghost"


def _commit() -> GitCommit:
    return GitCommit(
        sha="abcdef1234567",
        project="tl-agent/demo",
        author="john",
        committed_at=datetime(2026, 5, 22, 9, 30, tzinfo=UTC),
        message="fix publisher retry [ENG-1]",
        files_changed=2,
        insertions=10,
        deletions=3,
        linked_ticket_keys=("ENG-1",),
    )


def test_collect_jira_route_renders_and_persists_tickets(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def _fake_jira(selected: str) -> wf._JiraPull:
        return wf._JiraPull(
            "S-2026-05", [_ticket("ENG-1", "john"), _ticket("ENG-9", None)], None, 3, 10
        )

    monkeypatch.setattr(wf, "_collect_jira", _fake_jira)

    r = client.post("/workflow/collect_jira", data={"date": "2026-05-22"})
    assert r.status_code == 200
    assert "S-2026-05" in r.text
    assert "ENG-1" in r.text and "ENG-9" in r.text
    assert "⚠ 1 unassigned" in r.text  # ENG-9 has no assignee

    # Persisted: a later cached fragment shows the same tickets, no fetch.
    r2 = client.get("/workflow/jira_fragment?date=2026-05-22")
    assert r2.status_code == 200
    assert "ENG-1" in r2.text and "S-2026-05" in r2.text


def test_collect_gitlab_route_renders_and_persists_commits(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tl_agent.models import CollectionManifest

    async def _fake_gitlab(
        selected: str, since: datetime, until: datetime
    ) -> tuple[list[GitCommit], CollectionManifest, str | None]:
        return [_commit()], CollectionManifest(gitlab_groups=("tl-agent",)), None

    monkeypatch.setattr(wf, "_collect_gitlab", _fake_gitlab)

    r = client.post("/workflow/collect_gitlab", data={"date": "2026-05-22"})
    assert r.status_code == 200
    assert "GitLab — commits" in r.text
    assert "fix publisher retry" in r.text
    assert "abcdef12" in r.text

    # Persisted: the cached fragment shows the commit without a fetch.
    r2 = client.get("/workflow/gitlab_fragment?date=2026-05-22")
    assert r2.status_code == 200
    assert "abcdef12" in r2.text


def test_collect_gitlab_route_shows_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tl_agent.models import CollectionManifest

    async def _fake_gitlab(
        selected: str, since: datetime, until: datetime
    ) -> tuple[list[GitCommit], CollectionManifest, str | None]:
        return [], CollectionManifest(), "gitlab project discovery failed for group 'tl-agent'"

    monkeypatch.setattr(wf, "_collect_gitlab", _fake_gitlab)

    r = client.post("/workflow/collect_gitlab", data={"date": "2026-05-22"})
    assert r.status_code == 200
    assert "GitLab fetch failed" in r.text
    assert "discovery failed" in r.text


def test_run_prompt_reflects_cache_state(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing cached yet → only "Collect fresh", reuse disabled.
    r = client.get("/workflow/run_prompt?date=2026-05-22")
    assert r.status_code == 200
    assert "Collect fresh" in r.text
    assert "Reuse stored" not in r.text

    # Collect Jira, then the prompt offers reuse.
    async def _fake_jira(selected: str) -> wf._JiraPull:
        return wf._JiraPull("S-2026-05", [_ticket("ENG-1", "john")], None, 3, 10)

    monkeypatch.setattr(wf, "_collect_jira", _fake_jira)
    client.post("/workflow/collect_jira", data={"date": "2026-05-22"})

    r2 = client.get("/workflow/run_prompt?date=2026-05-22")
    assert "Reuse stored" in r2.text
    assert "1" in r2.text  # tickets count


# -------------------- standup collect + parse --------------------


def test_collect_standup_route_groups_segments_per_engineer(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    john = Engineer(id="john", display_name="John Doe")

    async def _fake_segments(
        selected: str, since: datetime, until: datetime
    ) -> tuple[list[dict[str, object]], str | None]:
        segments = [
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
        return [{"engineer": john, "segments": segments}], None

    monkeypatch.setattr(wf, "_collect_standup_segments", _fake_segments)

    r = client.post("/workflow/collect_standup", data={"date": "2026-05-22"})
    assert r.status_code == 200
    assert "John Doe" in r.text
    assert "Working on ENG-12 today." in r.text
    assert "Also, check out this cool article on Rust!" in r.text
    assert "off-topic" in r.text.lower()


def test_collect_standup_route_shows_chat_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_segments(
        selected: str, since: datetime, until: datetime
    ) -> tuple[list[dict[str, object]], str | None]:
        return [], "channel not found"

    monkeypatch.setattr(wf, "_collect_standup_segments", _fake_segments)

    r = client.post("/workflow/collect_standup", data={"date": "2026-05-22"})
    assert r.status_code == 200
    assert "channel not found" in r.text

"""Workflow tab — milestone projection + the route surface.

`_milestones` is pure and tested directly. The routes are exercised with a
FastAPI TestClient over a temp SQLite DB; the background pipeline launch is
monkeypatched so tests stay deterministic (no network, no event-loop juggling).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tl_agent.storage import connect, initialize
from tl_agent.web.routes import workflow as wf
from tl_agent.web.routes.workflow import _milestones

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

    scheduled: list[tuple[str, str | None]] = []

    def _fake_schedule(run_date: date, sprint_id: str | None = None) -> None:
        scheduled.append((run_date.isoformat(), sprint_id))

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
    assert client.scheduled == [("2026-05-22", None)]  # type: ignore[attr-defined]


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
    assert client.scheduled == [("2026-05-22", "S-2026-05")]  # type: ignore[attr-defined]

    conn = connect(Path(db))
    row = conn.execute("SELECT status, notes FROM runs WHERE id = 'run-await'").fetchone()
    conn.close()
    assert row["status"] == "resolved"
    assert json.loads(row["notes"])["sprint_decision"]["chosen"] == "S-2026-05"

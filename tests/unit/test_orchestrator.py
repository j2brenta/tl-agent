"""Orchestrator pipeline-wiring tests.

The phases themselves are stubbed (network + LLM live in their own tests); here
we assert the run-lifecycle bookkeeping the Workflow UI depends on: a run is
checkpointed to the `runs` table *after every phase*, so the live milestone
poll advances instead of sitting frozen on "running" until the very end.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from tl_agent.phases import orchestrator
from tl_agent.phases._context import RunContext
from tl_agent.phases.phase7_compose import Brief
from tl_agent.phases.sprint_select import SprintSelection
from tl_agent.storage import connect, initialize, load_team


def _signals() -> SimpleNamespace:
    return SimpleNamespace(
        commits=[], standups_today=[], sprint_tickets=[], tickets_added_since_yesterday=[]
    )


def _persisted_phases(conn: sqlite3.Connection, run_id: str) -> list[str]:
    row = conn.execute("SELECT notes FROM runs WHERE id = ?", (run_id,)).fetchone()
    return [p["phase"] for p in json.loads(row["notes"])["phases"]]


async def test_pipeline_checkpoints_after_each_phase(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = connect(tmp_path / "orch.db")
    initialize(conn)

    ctx = RunContext(
        run_id="run-test",
        run_date=date(2026, 5, 22),
        sqlite=conn,
        router=SimpleNamespace(),  # phases are stubbed; never dereferenced
        team=load_team(),
        idempotency=SimpleNamespace(),
        budget=SimpleNamespace(),
    )

    seen_at_phase3: list[str] = []

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    async def _sprint(_ctx: Any) -> SprintSelection:
        return SprintSelection(state="auto", chosen_sprint_id="S-1", reason="test")

    async def _collect(_ctx: Any) -> SimpleNamespace:
        return _signals()

    async def _correlate(*_a: Any, **_k: Any) -> list[Any]:
        # Capture what's already persisted by the time phase 3 runs — earlier
        # phases must be visible to the milestone poll, not buffered to the end.
        seen_at_phase3.extend(_persisted_phases(conn, "run-test"))
        return []

    async def _reconcile(*_a: Any, **_k: Any) -> SimpleNamespace:
        return SimpleNamespace(hotspots=[], closed_flag_ids=[])

    async def _list(*_a: Any, **_k: Any) -> list[Any]:
        return []

    async def _compose(*_a: Any, **_k: Any) -> Brief:
        return Brief(decisions=[])

    monkeypatch.setattr(orchestrator.sprint_select, "run", _sprint)
    monkeypatch.setattr(orchestrator.phase0_loop_closure, "run", _noop)
    monkeypatch.setattr(orchestrator.phase1_collect, "run", _collect)
    monkeypatch.setattr(orchestrator.phase2_triage, "run", _list)
    monkeypatch.setattr(orchestrator.phase3_correlate, "run", _correlate)
    monkeypatch.setattr(orchestrator.phase4_reconcile, "run", _reconcile)
    monkeypatch.setattr(orchestrator.phase5_deepdive, "run", _list)
    monkeypatch.setattr(orchestrator.phase6_response_mode, "run", _list)
    monkeypatch.setattr(orchestrator.phase7_compose, "run", _compose)

    result = await orchestrator._run_pipeline(ctx)

    # Mid-run visibility: phases 0/1/2 were already in the DB when phase 3 ran.
    assert "phase1_collect" in seen_at_phase3
    assert "phase2_triage" in seen_at_phase3

    # End state: completed, finished_at stamped, all phases recorded.
    row = conn.execute(
        "SELECT status, finished_at FROM runs WHERE id = ?", (result.run_id,)
    ).fetchone()
    assert row["status"] == "completed"
    assert row["finished_at"] is not None
    assert "phase7_compose" in _persisted_phases(conn, result.run_id)
    conn.close()

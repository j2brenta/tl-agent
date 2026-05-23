"""Per-run context bundle the orchestrator passes through every phase.

Keeping the shared deps in one frozen dataclass means each phase signature
stays small and the wiring lives in one place (the orchestrator).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date

from tl_agent.llm.budget import BudgetTracker
from tl_agent.llm.router import Router
from tl_agent.storage import TeamConfig
from tl_agent.tools import SqliteIdempotencyStore


@dataclass
class RunContext:
    """All the shared state for one orchestrator run."""

    run_id: str
    run_date: date
    sqlite: sqlite3.Connection
    router: Router
    team: TeamConfig
    idempotency: SqliteIdempotencyStore
    budget: BudgetTracker
    project: str = "tl-agent/demo"
    standup_channel_id: str = "town-square"
    sprint_id: str | None = None
    notes: list[str] = field(default_factory=list[str])

    @property
    def run_date_iso(self) -> str:
        return self.run_date.isoformat()

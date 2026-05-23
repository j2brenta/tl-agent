"""Eval case schema.

Each case in `cases/*.yaml` parses into a `Case`. The runner loads the case,
applies the seed_state, runs the orchestrator (or a single phase), and
asserts the expected_outcomes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from evals.failure_taxonomy import FailureKind


class ExpectedOutcomes(BaseModel):
    """The assertions the case enforces against the orchestrator output."""

    model_config = ConfigDict(extra="forbid")

    # Counts the brief must produce
    min_decisions: int = 0
    max_decisions: int | None = None
    # Specific hot-spot identities expected (matched by substring in summary)
    expected_hotspot_summaries: list[str] = Field(default_factory=list[str])
    # Per-decision assertions
    expected_modes: dict[str, str] = Field(default_factory=dict[str, str])
    # Maximum allowed open flags at end-of-run
    max_open_flags: int | None = None
    # Notes the run must NOT contain (substring match)
    forbidden_notes: list[str] = Field(default_factory=list[str])


class SeedState(BaseModel):
    """How to set up the eval environment before running the orchestrator."""

    model_config = ConfigDict(extra="forbid")

    # Override jira_mock state.json for this case (relative to case dir)
    jira_fixture: str | None = None
    # Override standups.yaml (relative to case dir)
    standups_fixture: str | None = None
    # Pre-seed daily_flags rows (so reconcile can carry them forward)
    preseed_flags: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])


class Case(BaseModel):
    """One eval scenario."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    title: str
    description: str = ""
    suite: list[str] = Field(default_factory=list[str])  # e.g. ["quick", "full"]
    run_date: str  # YYYY-MM-DD
    failure_kind: FailureKind  # the kind this case guards against
    seed_state: SeedState = Field(default_factory=SeedState)
    expected: ExpectedOutcomes

    @classmethod
    def load(cls, path: Path) -> Case:
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

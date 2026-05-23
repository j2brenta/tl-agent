"""Prediction — a claim the agent made yesterday that can be checked today.

Phase 0 (loop closure) resolves yesterday's predictions against the latest
state. This is the cheapest way to keep the system honest: every flag becomes
a falsifiable claim, and we measure resolution rate as an eval signal.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class PredictionOutcome(StrEnum):
    """How a prediction resolved when checked the next morning."""

    OPEN = "open"  # not yet evaluable
    CORRECT = "correct"
    INCORRECT = "incorrect"
    UNRESOLVABLE = "unresolvable"  # missing data to judge


class Prediction(BaseModel):
    """A check-back-tomorrow claim."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    made_on: date
    claim: str = Field(min_length=1, max_length=240)
    related_hotspot_id: str | None = None
    resolve_after: date
    outcome: PredictionOutcome = PredictionOutcome.OPEN
    resolved_on: date | None = None
    resolution_note: str | None = None

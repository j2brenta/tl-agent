"""The failure-mode taxonomy for the agent.

When an eval case fails, the runner classifies the failure into one of
these categories. Per-category pass rate is reported alongside the overall
rate. Keeping the set small + exhaustive is the senior signal: "we know
how this thing fails" beats "we have a bunch of failing tests".

Tighten / extend with care. Adding a new kind without updating the runner's
classifier silently leaves it unused.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FailureKind(StrEnum):
    """Coarse buckets for eval failures."""

    TOOL_ARG_HALLUCINATION = "tool_arg_hallucination"  # call with args not in schema
    PREMATURE_STOP = "premature_stop"  # claimed done with missing investigation
    INFINITE_LOOP = "infinite_loop"  # would have looped (no-progress / repetition)
    WRONG_TOOL_SELECTION = "wrong_tool_selection"  # used a tool that can't answer the question
    MISSED_HOTSPOT = "missed_hotspot"  # expected hot spot not surfaced
    FALSE_POSITIVE = "false_positive"  # flagged a hot spot that shouldn't be one
    RESPONSE_MODE_MISMATCH = "response_mode_mismatch"  # wrong mode for the situation
    IDEMPOTENCY_VIOLATION = "idempotency_violation"  # writer sent twice
    READBACK_FAILURE = "readback_failure"  # post landed but readback disagreed
    ESCALATION_MISS = "escalation_miss"  # should have escalated, didn't
    OTHER = "other"  # unclassified — review manually


@dataclass(frozen=True)
class FailureSummary:
    """One bucket of failure counts."""

    kind: FailureKind
    count: int
    examples: tuple[str, ...]  # case ids


def acceptance_thresholds() -> dict[FailureKind, float]:
    """Per-category minimum pass rates the eval-full target enforces.

    Tightened over time as the agent improves. Numbers are deliberate; do
    not bump without a discussion + a corresponding change-log entry.
    """
    return {
        FailureKind.TOOL_ARG_HALLUCINATION: 0.95,
        FailureKind.PREMATURE_STOP: 0.85,
        FailureKind.INFINITE_LOOP: 0.95,
        FailureKind.WRONG_TOOL_SELECTION: 0.80,
        FailureKind.MISSED_HOTSPOT: 0.85,
        FailureKind.FALSE_POSITIVE: 0.85,
        FailureKind.RESPONSE_MODE_MISMATCH: 0.80,
        FailureKind.IDEMPOTENCY_VIOLATION: 1.00,  # zero tolerance
        FailureKind.READBACK_FAILURE: 1.00,  # zero tolerance
        FailureKind.ESCALATION_MISS: 0.90,
        FailureKind.OTHER: 0.60,
    }

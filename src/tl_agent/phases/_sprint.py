"""Sprint-progress arithmetic.

Jira's Agile API reports a sprint's `startDate`/`endDate` but *not* "which
day of the sprint is it" or "how long is the sprint" — those are derived.
We derive them from the deterministic run date (never `datetime.now`) so
eval replays reproduce exactly.
"""

from __future__ import annotations

from datetime import date, datetime

_DEFAULT_DAY = 1
_DEFAULT_LENGTH = 10


def _as_date(value: datetime | date) -> date:
    return value.date() if isinstance(value, datetime) else value


def sprint_progress(
    start: datetime | date | None,
    end: datetime | date | None,
    run_date: date,
) -> tuple[int, int]:
    """Return `(sprint_day, sprint_length_days)` for `run_date`.

    `sprint_day` is 1-based and clamped to `[1, length]`; missing dates fall
    back to sensible defaults so a partial sprint payload never crashes a run.
    """
    if start is None or end is None:
        return _DEFAULT_DAY, _DEFAULT_LENGTH
    start_d = _as_date(start)
    end_d = _as_date(end)
    length = max((end_d - start_d).days, 1)
    day = (run_date - start_d).days + 1
    day = max(1, min(day, length))
    return day, length

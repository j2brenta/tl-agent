"""Tests for sprint day/length derivation."""

from __future__ import annotations

from datetime import UTC, date, datetime

from tl_agent.phases._sprint import sprint_progress


def test_mid_sprint_day_is_one_based() -> None:
    start = datetime(2026, 5, 19, 9, 0, tzinfo=UTC)
    end = datetime(2026, 5, 29, 9, 0, tzinfo=UTC)
    day, length = sprint_progress(start, end, date(2026, 5, 22))
    assert (day, length) == (4, 10)


def test_first_day_is_one() -> None:
    start = datetime(2026, 5, 19, 9, 0, tzinfo=UTC)
    end = datetime(2026, 5, 29, 9, 0, tzinfo=UTC)
    day, _ = sprint_progress(start, end, date(2026, 5, 19))
    assert day == 1


def test_day_clamped_to_length() -> None:
    start = datetime(2026, 5, 19, tzinfo=UTC)
    end = datetime(2026, 5, 29, tzinfo=UTC)
    # Run date past the end → clamped, never exceeds length.
    day, length = sprint_progress(start, end, date(2026, 6, 5))
    assert day == length == 10


def test_before_start_clamped_to_one() -> None:
    start = datetime(2026, 5, 19, tzinfo=UTC)
    end = datetime(2026, 5, 29, tzinfo=UTC)
    day, _ = sprint_progress(start, end, date(2026, 5, 10))
    assert day == 1


def test_missing_dates_fall_back_to_defaults() -> None:
    assert sprint_progress(None, None, date(2026, 5, 22)) == (1, 10)
    assert sprint_progress(datetime(2026, 5, 19, tzinfo=UTC), None, date(2026, 5, 22)) == (1, 10)

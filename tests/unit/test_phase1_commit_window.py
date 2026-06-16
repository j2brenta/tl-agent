"""gitlab_commit_window expands the lookback to 3 days on Mondays."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from tl_agent.phases.phase1_collect import gitlab_commit_window
from tl_agent.storage.markdown_loader import TeamConfig


def _team(monday_weekend_lookback: bool = True) -> TeamConfig:
    return TeamConfig(members=(), monday_weekend_lookback=monday_weekend_lookback)


def _until(d: date) -> datetime:
    return datetime.combine(d, time(12, 0), tzinfo=UTC)


MONDAY = date(2026, 6, 15)  # known Monday
TUESDAY = date(2026, 6, 16)
FRIDAY = date(2026, 6, 12)


def test_monday_extends_lookback_to_friday() -> None:
    since, until = gitlab_commit_window(MONDAY, _team())
    assert until == _until(MONDAY)
    assert since == _until(MONDAY) - timedelta(days=3)


def test_non_monday_uses_single_day_lookback() -> None:
    since, until = gitlab_commit_window(TUESDAY, _team())
    assert until == _until(TUESDAY)
    assert since == _until(TUESDAY) - timedelta(days=1)


def test_monday_lookback_disabled_uses_single_day() -> None:
    since, _ = gitlab_commit_window(MONDAY, _team(monday_weekend_lookback=False))
    assert since == _until(MONDAY) - timedelta(days=1)


def test_friday_is_not_extended() -> None:
    since, _ = gitlab_commit_window(FRIDAY, _team())
    assert since == _until(FRIDAY) - timedelta(days=1)


@pytest.mark.parametrize("weekday_offset", [1, 2, 3, 4, 5, 6])
def test_only_monday_is_extended(weekday_offset: int) -> None:
    # MONDAY + offset covers Tue through Sun — none should extend.
    d = MONDAY + timedelta(days=weekday_offset)
    since, until = gitlab_commit_window(d, _team())
    assert until - since == timedelta(days=1)

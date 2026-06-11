"""Phase 1 folds a ticket's raw Jira assignee/reporter onto the team roster."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from tl_agent.models import JiraStatus, JiraTicket
from tl_agent.phases.phase1_collect import _resolve_people
from tl_agent.storage import load_team


def _ticket(assignee: str | None, reporter: str | None = None) -> JiraTicket:
    return JiraTicket(
        key="ENG-1",
        summary="x",
        status=JiraStatus.IN_PROGRESS,
        assignee=assignee,
        reporter=reporter,
        created_at=datetime(2026, 5, 19, tzinfo=UTC),
        updated_at=datetime(2026, 5, 20, tzinfo=UTC),
    )


def _ctx() -> Any:
    return SimpleNamespace(team=load_team())


def test_display_name_and_alias_resolve_to_member_id() -> None:
    resolved = _resolve_people(_ticket("John Doe", reporter="dpark"), _ctx())
    assert resolved.assignee == "john"  # display name → id
    assert resolved.reporter == "dana"  # Jira handle alias → id


def test_account_id_resolves_via_jira_account_id() -> None:
    # When Jira surfaces only the account id, jira_account_id bridges it.
    resolved = _resolve_people(_ticket("matt"), _ctx())
    assert resolved.assignee == "matt"


def test_unresolved_assignee_is_left_untouched() -> None:
    ticket = _ticket("Outside Contractor")
    resolved = _resolve_people(ticket, _ctx())
    assert resolved is ticket  # no copy when nothing changes
    assert resolved.assignee == "Outside Contractor"

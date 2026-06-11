"""Identity-resolution shaping for the /team page.

Tests the pure `member_row` helper (no web server / DB needed) — it's where
the "this handle diverges from id" warning logic lives.
"""

from __future__ import annotations

from tl_agent.models import Engineer
from tl_agent.web.routes.team import member_row


def test_aligned_handles_are_not_at_risk() -> None:
    eng = Engineer(
        id="john",
        display_name="John Doe",
        jira_account_id="john",
        gitlab_username="john",
        chat_user_id="john",
    )
    row = member_row(eng)
    assert row["at_risk"] is False
    assert row["jira"]["diverges"] is False
    assert row["jira"]["handle"] == "john"


def test_divergent_jira_handle_flags_risk() -> None:
    pm = Engineer(
        id="dana",
        display_name="Dana Park",
        jira_account_id="dpark",
        gitlab_username="dpark",
        chat_user_id="dana",
        aliases=("dpark",),
    )
    row = member_row(pm)
    # Jira/GitLab divergence is a missed-work risk (matched by id).
    assert row["jira"]["diverges"] is True
    assert row["gitlab"]["diverges"] is True
    assert row["at_risk"] is True
    # Chat divergence would be alias-safe; here chat == id anyway.
    assert row["chat"]["diverges"] is False


def test_unset_handle_falls_back_to_id() -> None:
    eng = Engineer(id="karen", display_name="Karen Liu")
    row = member_row(eng)
    assert row["jira"]["explicit"] is False
    assert row["jira"]["handle"] == "karen"
    assert row["at_risk"] is False


def test_divergent_chat_handle_is_alias_safe() -> None:
    eng = Engineer(id="matt", display_name="Matt Stone", chat_user_id="matt_42")
    row = member_row(eng)
    assert row["chat"]["diverges"] is True
    assert row["chat"]["alias_safe"] is True
    # Chat divergence alone does not raise the id-matching risk flag.
    assert row["at_risk"] is False

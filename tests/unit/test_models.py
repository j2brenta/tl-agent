"""Sanity tests for the Pydantic model layer.

Goal: catch schema regressions early (renamed enum values, missing fields,
loosened constraints). Not exhaustive — model tests prove the contract holds,
not business logic.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from tl_agent.models import (
    ApprovalAction,
    DailySignals,
    Decision,
    Engineer,
    EngineerTriage,
    Evidence,
    EvidenceKind,
    Flag,
    FlagType,
    GitCommit,
    Hotspot,
    JiraStatus,
    JiraTicket,
    Prediction,
    PredictionOutcome,
    ResponseDraft,
    ResponseMode,
    Role,
    StandupMessage,
    TriageStatus,
)


def _now() -> datetime:
    return datetime(2026, 5, 22, 9, 0, tzinfo=UTC)


def test_engineer_matches_aliases() -> None:
    eng = Engineer(
        id="john",
        display_name="John Doe",
        gitlab_username="jdoe",
        aliases=("johnny", "jd"),
    )
    assert eng.matches("john")
    assert eng.matches("JDoe")
    assert eng.matches("johnny")
    assert not eng.matches("alicia")


def test_engineer_role_kind_defaults_to_engineer() -> None:
    eng = Engineer(id="john", display_name="John Doe")
    assert eng.role_kind is Role.ENGINEER


def test_engineer_accepts_explicit_role_kind() -> None:
    pm = Engineer(id="dana", display_name="Dana Park", role_kind=Role.PRODUCT_MANAGER)
    assert pm.role_kind is Role.PRODUCT_MANAGER
    # Parses from the raw string the markdown loader produces.
    tl = Engineer.model_validate(
        {"id": "kirill", "display_name": "Kirill", "role_kind": "team_lead"}
    )
    assert tl.role_kind is Role.TEAM_LEAD


def test_triage_attention_worthy() -> None:
    t = EngineerTriage(
        engineer_id="matt",
        status=TriageStatus.YELLOW,
        one_line_reason="3-day-old blocker, no movement",
        evidence=[Evidence(kind=EvidenceKind.TICKET, ref="ENG-12", note="In review since Tue")],
    )
    assert t.is_attention_worthy
    t2 = t.model_copy(update={"status": TriageStatus.GREEN})
    assert not t2.is_attention_worthy


def test_triage_rejects_long_reason() -> None:
    with pytest.raises(ValidationError):
        EngineerTriage(
            engineer_id="matt",
            status=TriageStatus.RED,
            one_line_reason="x" * 1000,
        )


def test_flag_days_hot_ge_1() -> None:
    with pytest.raises(ValidationError):
        Flag(
            id="f1",
            type=FlagType.INDIVIDUAL,
            title="quiet engineer",
            status=TriageStatus.YELLOW,
            days_hot=0,
            first_seen=date(2026, 5, 21),
            last_seen=date(2026, 5, 22),
        )


def test_hotspot_evidence_cap() -> None:
    too_many = [Evidence(kind=EvidenceKind.COMMIT, ref=f"sha{i:04d}", note="x") for i in range(13)]
    with pytest.raises(ValidationError):
        Hotspot(
            id="h1",
            type=FlagType.INDIVIDUAL,
            summary="too much evidence",
            severity=TriageStatus.YELLOW,
            evidence=too_many,
        )


def test_prediction_open_default() -> None:
    p = Prediction(
        id="p1",
        made_on=date(2026, 5, 22),
        claim="ENG-12 will close by EOD",
        resolve_after=date(2026, 5, 23),
    )
    assert p.outcome is PredictionOutcome.OPEN


def test_decision_round_trips_response_mode() -> None:
    d = Decision(
        id="d1",
        created_at=_now(),
        run_date="2026-05-26",
        hotspot_id="h1",
        proposed_mode=ResponseMode.DM,
        proposed_body="hey, can we sync on ENG-12?",
        tl_action=ApprovalAction.APPROVED,
        tl_acted_at=_now(),
    )
    dumped = d.model_dump_json()
    restored = Decision.model_validate_json(dumped)
    assert restored == d


def test_response_draft_target_optional() -> None:
    draft = ResponseDraft(
        hotspot_id="h1",
        mode=ResponseMode.STANDUP,
        body="we should talk about ENG-12 in standup",
        rationale="team-wide pattern",
    )
    assert draft.target is None


def test_daily_signals_aggregate() -> None:
    sig = DailySignals(
        run_date="2026-05-22",
        sprint_day=4,
        sprint_length_days=10,
        standups_today=[
            StandupMessage(engineer_id="john", date_iso="2026-05-22", raw="working on ENG-12")
        ],
        sprint_tickets=[
            JiraTicket(
                key="ENG-12",
                summary="add retry to publisher",
                status=JiraStatus.IN_PROGRESS,
                assignee="john",
                created_at=_now(),
                updated_at=_now(),
            )
        ],
        commits=[
            GitCommit(
                sha="abc1234",
                project="tl-agent/demo",
                author="john",
                committed_at=_now(),
                message="ENG-12 wip",
                files_changed=3,
                insertions=42,
                deletions=10,
                linked_ticket_keys=("ENG-12",),
            )
        ],
    )
    assert sig.sprint_day == 4
    assert sig.standups_today[0].engineer_id == "john"
    assert sig.commits[0].linked_ticket_keys == ("ENG-12",)

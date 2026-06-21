"""Deterministic seed for jira_mock.

Run once after `docker compose up jira_mock` (or inside the container at
first boot). Writes `fixtures/state.json` with:
  - ~30 tickets across an active sprint
  - A few status-history entries (so get_ticket_history returns real shape)
  - Dependency links (ENG-12 blocked_by ENG-9; ENG-19 blocked_by ENG-12)

Why deterministic: evals depend on the same starting state every run, so the
default anchor is fixed (2026-05-22). For a live demo, set
`JIRA_ANCHOR_DATE=YYYY-MM-DD` to move the active sprint near a chosen run day
(the GitLab `COMMIT_ANCHOR_DATE` analogue) — the sprint then starts 3 days
before the anchor, mirroring the default day-3 position so a workflow run for
that date sees a genuinely active sprint. After re-seeding, restart the mock
(it reads `state.json` on startup):
    JIRA_ANCHOR_DATE=2026-06-21 uv run python -m services.jira_mock.seed
    docker compose restart jira_mock
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

# Default eval-stable anchor; override with JIRA_ANCHOR_DATE for a live demo.
_DEFAULT_RUN_DATE = date(2026, 5, 22)


def _anchors() -> tuple[datetime, datetime]:
    """Return (RUN_DATE, SPRINT_START), shifted to JIRA_ANCHOR_DATE if set."""
    anchor = os.environ.get("JIRA_ANCHOR_DATE")
    run_day = date.fromisoformat(anchor) if anchor else _DEFAULT_RUN_DATE
    run_date = datetime.combine(run_day, time(9, 0), tzinfo=UTC)
    return run_date, run_date - timedelta(days=3)


RUN_DATE, SPRINT_START = _anchors()

BOARD_ID = "ENG"
SPRINT_ID = "S-2026-05"
SPRINT_NAME = "Eng Sprint 19"

ENGINEERS = ["john", "matt", "alicia", "karen"]
TICKETS = [
    # John — billing
    ("ENG-1", "john", "in_progress", 3, "rate limiter for /charges"),
    ("ENG-2", "john", "in_review", 2, "retry envelope for stripe webhooks"),
    ("ENG-12", "john", "in_progress", 5, "publisher retry policy"),  # the hot one
    ("ENG-19", "john", "blocked", 3, "billing dashboard widget"),
    # Matt — auth
    ("ENG-3", "matt", "in_progress", 2, "refresh token rotation"),
    ("ENG-9", "matt", "in_progress", 3, "auth events stream"),
    ("ENG-15", "matt", "todo", 1, "audit log cleanup"),
    # Alicia — frontend
    ("ENG-4", "alicia", "in_progress", 3, "dashboard chart refresh"),
    ("ENG-7", "alicia", "todo", 2, "admin page filters"),
    ("ENG-22", "alicia", "in_review", 2, "table virtualization"),
    # Karen — ingestion / notifications
    ("ENG-5", "karen", "in_progress", 3, "kafka consumer lag alert"),
    ("ENG-10", "karen", "todo", 2, "slack notifier"),
    ("ENG-25", "karen", "in_progress", 1, "log levels per service"),
]


def build_state() -> dict[str, object]:
    tickets: dict[str, object] = {}
    history: dict[str, list[object]] = {}
    links: dict[str, object] = {}

    for key, assignee, status, points, summary in TICKETS:
        created = SPRINT_START
        updated = RUN_DATE - timedelta(hours=2)
        tickets[key] = {
            "key": key,
            "summary": summary,
            "status": status,
            "assignee": assignee,
            "reporter": "tl",
            "points": float(points),
            "sprint": SPRINT_ID,
            "created_at": created.isoformat(),
            "updated_at": updated.isoformat(),
            "added_to_sprint_at": None,
            "blocks": [],
            "blocked_by": [],
            "status_history": [],
            "estimate_history": [],
            "labels": [],
        }
        # A couple of status moves for the long-running ones.
        if key in {"ENG-12", "ENG-9"}:
            history[key] = [
                {
                    "at": (SPRINT_START + timedelta(days=1)).isoformat(),
                    "by": assignee,
                    "from_status": "todo",
                    "to_status": "in_progress",
                }
            ]

    # Dependency hot spot: ENG-9 blocks ENG-12 blocks ENG-19.
    links["ENG-9"] = {"blocks": ["ENG-12"], "blocked_by": []}
    links["ENG-12"] = {"blocks": ["ENG-19"], "blocked_by": ["ENG-9"]}
    links["ENG-19"] = {"blocks": [], "blocked_by": ["ENG-12"]}

    # The active sprint's ticket list. Sprint day/length are derived downstream
    # from the board metadata's start/end dates, not stored here.
    sprint = {
        "sprint_id": SPRINT_ID,
        "issues": list(tickets.values()),
    }

    # Board-level sprint list (metadata only). Exactly one `active` sprint whose
    # name matches the team pattern, so discovery auto-resolves on the default
    # seed. The closed/future entries exercise the filter without ambiguity.
    sprints = [
        {
            "id": "S-2026-04",
            "name": "Eng Sprint 18",
            "state": "closed",
            "board_id": BOARD_ID,
            "start_date": (SPRINT_START - timedelta(days=14)).isoformat(),
            "end_date": (SPRINT_START - timedelta(days=4)).isoformat(),
        },
        {
            "id": SPRINT_ID,
            "name": SPRINT_NAME,
            "state": "active",
            "board_id": BOARD_ID,
            "start_date": SPRINT_START.isoformat(),
            "end_date": (SPRINT_START + timedelta(days=10)).isoformat(),
        },
        {
            "id": "S-2026-06",
            "name": "Eng Sprint 20",
            "state": "future",
            "board_id": BOARD_ID,
            "start_date": (SPRINT_START + timedelta(days=11)).isoformat(),
            "end_date": (SPRINT_START + timedelta(days=21)).isoformat(),
        },
    ]

    boards = [{"id": BOARD_ID, "name": "Engineering", "type": "scrum", "project_key": "ENG"}]

    return {
        "tickets": tickets,
        "history": history,
        "links": links,
        "sprint": sprint,
        "board_id": BOARD_ID,
        "boards": boards,
        "sprints": sprints,
    }


def main() -> None:
    out = Path(__file__).parent / "fixtures" / "state.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(build_state(), indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {out} with {len(TICKETS)} tickets")


if __name__ == "__main__":
    main()
elif os.environ.get("JIRA_MOCK_AUTOSEED") == "1":
    # When set inside the container, seed on import.
    main()

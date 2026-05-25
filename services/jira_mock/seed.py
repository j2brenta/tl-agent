"""Deterministic seed for jira_mock.

Run once after `docker compose up jira_mock` (or inside the container at
first boot). Writes `fixtures/state.json` with:
  - ~30 tickets across an active sprint
  - A few status-history entries (so get_ticket_history returns real shape)
  - Dependency links (ENG-12 blocked_by ENG-9; ENG-19 blocked_by ENG-12)

Why deterministic: evals depend on the same starting state every run.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Anchor today to a fixed date so eval scenarios reproduce.
RUN_DATE = datetime(2026, 5, 22, 9, 0, tzinfo=UTC)
SPRINT_START = datetime(2026, 5, 19, 9, 0, tzinfo=UTC)

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
            "sprint": "S-2026-05",
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

    sprint = {
        "sprint_id": "S-2026-05",
        "sprint_day": 4,
        "sprint_length_days": 10,
        "added_since": (RUN_DATE - timedelta(days=1)).isoformat(),
        "issues": list(tickets.values()),
    }

    return {"tickets": tickets, "history": history, "links": links, "sprint": sprint}


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

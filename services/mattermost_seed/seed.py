"""Seed the in-compose Mattermost with team, channels, bot users, and standup history.

Uses the v4 REST API directly (no mmctl dependency). Idempotent — on re-run,
existing users/channels/posts are skipped.

Run once after `docker compose up mattermost` and Mattermost has finished
its first-boot wizard (about 30s).

Outputs an admin bot token at the end, which the agent uses as
TLA_MATTERMOST_TOKEN.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml
from dotenv import load_dotenv

# Load .env from the repo root so this script reads the same source of truth
# as the Python app + the bash seed scripts. Idempotent + safe if missing.
_REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".env", override=False)

URL = os.environ.get("TLA_MATTERMOST_URL", "http://localhost:8065")
ADMIN_USER = os.environ.get("TLA_MATTERMOST_ADMIN_USER", "tl-admin")
ADMIN_EMAIL = os.environ.get("TLA_MATTERMOST_ADMIN_EMAIL", "tl-admin@example.local")
ADMIN_PW = os.environ.get("TLA_MATTERMOST_ADMIN_PW", "Tl-AdminPw1!")
TEAM = os.environ.get("TLA_MATTERMOST_TEAM", "engineering")
STANDUP_CHANNEL = os.environ.get("TLA_MATTERMOST_STANDUP_CHANNEL", "town-square")
ENGINEERS = ["john", "matt", "alicia", "karen"]


def main() -> None:
    with httpx.Client(base_url=URL, timeout=15) as client:
        admin_token = _ensure_admin(client)
        client.headers["Authorization"] = f"Bearer {admin_token}"

        team_id = _ensure_team(client)
        for eng in ENGINEERS:
            _ensure_user(client, eng, team_id)

        channel_id = _ensure_channel(client, team_id, STANDUP_CHANNEL)
        _post_standups(client, channel_id)

        print(f"\nadmin token: {admin_token}")
        print(f"team_id: {team_id}")
        print(f"standup channel_id: {channel_id}\n")


# ---------- helpers ----------


def _ensure_admin(client: httpx.Client) -> str:
    # First-time install requires creating the initial admin user.
    me = client.get("/api/v4/users/me", headers={"Authorization": "Bearer none"})
    if me.status_code != 200:
        # Try to create — succeeds on a brand-new install, otherwise 403 (already initialised).
        client.post(
            "/api/v4/users",
            json={
                "email": ADMIN_EMAIL,
                "username": ADMIN_USER,
                "password": ADMIN_PW,
            },
        )
    login = client.post(
        "/api/v4/users/login",
        json={"login_id": ADMIN_USER, "password": ADMIN_PW},
    )
    login.raise_for_status()
    return str(login.headers.get("Token", ""))


def _ensure_team(client: httpx.Client) -> str:
    r = client.get(f"/api/v4/teams/name/{TEAM}")
    if r.status_code == 200:
        return str(r.json()["id"])
    r2 = client.post(
        "/api/v4/teams",
        json={"name": TEAM, "display_name": TEAM.title(), "type": "O"},
    )
    r2.raise_for_status()
    return str(r2.json()["id"])


def _ensure_user(client: httpx.Client, username: str, team_id: str) -> str:
    r = client.get(f"/api/v4/users/username/{username}")
    if r.status_code == 200:
        user_id = str(r.json()["id"])
    else:
        r2 = client.post(
            "/api/v4/users",
            json={
                "email": f"{username}@example.local",
                "username": username,
                "password": "Eng-Pw1!",
            },
        )
        r2.raise_for_status()
        user_id = str(r2.json()["id"])
    # Add to team (idempotent)
    client.post(f"/api/v4/teams/{team_id}/members", json={"team_id": team_id, "user_id": user_id})
    return user_id


def _ensure_channel(client: httpx.Client, team_id: str, name: str) -> str:
    r = client.get(f"/api/v4/teams/{team_id}/channels/name/{name}")
    if r.status_code == 200:
        return str(r.json()["id"])
    r2 = client.post(
        "/api/v4/channels",
        json={"team_id": team_id, "name": name, "display_name": name.title(), "type": "O"},
    )
    r2.raise_for_status()
    return str(r2.json()["id"])


def _post_standups(client: httpx.Client, channel_id: str) -> None:
    """Post 7 days x 4 engineers from fixtures/standups.yaml."""
    fixtures = Path(__file__).parent / "fixtures" / "standups.yaml"
    if not fixtures.exists():
        print("(no standups.yaml — skipping standup seed)", file=sys.stderr)
        return
    data: dict[str, Any] = yaml.safe_load(fixtures.read_text(encoding="utf-8"))
    for entry in data.get("standups", []):
        username = entry["engineer"]
        # Look up user_id once per username
        user = client.get(f"/api/v4/users/username/{username}").json()
        # Post as that user via system-admin impersonation (REST allows acting_user_id)
        client.post(
            "/api/v4/posts",
            json={
                "channel_id": channel_id,
                "message": entry["message"],
                "user_id": user["id"],
            },
        )


if __name__ == "__main__":
    main()

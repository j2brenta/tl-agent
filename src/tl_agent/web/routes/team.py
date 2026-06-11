"""Team roster — read-only view of who's who and how they resolve per system.

Route:
  GET /team   — the full roster grouped into Leadership (team lead, product
                manager) and Engineers, with each person's Jira / GitLab / chat
                handle and a warning where a handle diverges from the `id`.

Why the warnings matter: Phase 2 triage and the sprint page match work to
people with `.id` equality (`ticket.assignee == eng.id`,
`commit.author == eng.id`) — *not* against `jira_account_id` /
`gitlab_username`. So a Jira/GitLab handle that differs from `id` means that
person's tickets/commits can silently go unmatched. Chat is safer: Phase 1
resolves messages via `Engineer.matches()`, which also checks aliases.

This page only reads `config/team.md`; editing stays in the file + git
(LAYER 1 is never overwritten by the agent).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from tl_agent.models import Engineer
from tl_agent.storage.markdown_loader import load_team

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=select_autoescape())


def _system_cell(handle: str | None, member_id: str, *, alias_safe: bool) -> dict[str, Any]:
    """Resolution status for one system (Jira / GitLab / chat).

    `diverges` is True when an explicit handle differs from `id`. `alias_safe`
    marks systems (chat) where `Engineer.matches()` would still resolve via an
    alias, so divergence there is benign rather than a missed-work risk.
    """
    effective = handle if handle is not None else member_id
    diverges = handle is not None and handle.lower() != member_id.lower()
    return {
        "handle": effective,
        "explicit": handle is not None,
        "diverges": diverges,
        "alias_safe": alias_safe,
    }


def member_row(member: Engineer) -> dict[str, Any]:
    """Shape one roster member into the cells the template renders."""
    jira = _system_cell(member.jira_account_id, member.id, alias_safe=False)
    gitlab = _system_cell(member.gitlab_username, member.id, alias_safe=False)
    chat = _system_cell(member.chat_user_id, member.id, alias_safe=True)
    return {
        "member": member,
        "jira": jira,
        "gitlab": gitlab,
        "chat": chat,
        "aliases": member.aliases,
        # A risk only where divergence is not alias-covered (Jira / GitLab).
        "at_risk": jira["diverges"] or gitlab["diverges"],
    }


@router.get("/team", response_class=HTMLResponse)
async def team() -> HTMLResponse:
    config = load_team()
    leadership = [m for m in (config.team_lead, config.product_manager) if m is not None]
    template = _env.get_template("team.html")
    return HTMLResponse(
        template.render(
            leadership=[member_row(m) for m in leadership],
            engineers=[member_row(m) for m in config.engineers],
        )
    )

"""Settings & diagnostics — read-only view of every configured backend, with a
live reachability probe for each, so "why did X just fail?" is answerable at a
glance.

Route:
  GET /settings — masked config values + live health probes (Jira, GitLab,
  chat, Ollama, Phoenix/OTLP, SQLite) and the active LLM router map (which
  phase runs on which provider/model). A banner flags any backend the router
  actually depends on that is unreachable — e.g. an Ollama router config while
  the Ollama container is down.

Reads `Settings` + the router config only; never mutates. Secrets are masked.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from tl_agent.settings import Settings, get_settings

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
_env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=select_autoescape())

_PROBE_TIMEOUT = 2.5


def _mask(secret: str) -> str:
    """Render a secret as presence + a short tail; never the whole value."""
    if not secret:
        return "— not set"
    tail = secret[-4:] if len(secret) > 8 else ""
    return f"set ·…{tail}" if tail else "set"


async def _probe(url: str, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """GET `url`; any HTTP response means the host is reachable.

    A transport error (DNS failure, connection refused, timeout) is the real
    signal we care about. 5xx is reachable-but-degraded.
    """
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
            resp = await client.get(url, headers=headers or {})
    except Exception as exc:  # surface any failure as "down"
        return {"ok": False, "detail": type(exc).__name__, "url": url}
    return {"ok": resp.status_code < 500, "detail": f"HTTP {resp.status_code}", "url": url}


def _router_view() -> dict[str, Any]:
    """Active router config path + per-phase (provider, model) map."""
    from tl_agent.llm.router import build_default

    r = build_default()
    routes = [
        {"phase": name, "provider": rt.provider, "model": rt.model}
        for name, rt in sorted(r.routes.items())
    ]
    used = sorted({rt.provider for rt in r.routes.values()})
    return {"config_path": str(r.config_path), "routes": routes, "used_providers": used}


def _storage_rows(s: Settings) -> list[dict[str, Any]]:
    from tl_agent.storage import connect

    rows: list[dict[str, Any]] = [{"label": "SQLite path", "value": str(s.sqlite_path)}]
    try:
        conn = connect(s.sqlite_path)
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        ver_row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        conn.close()
    except Exception as exc:
        rows.append(
            {
                "label": "Database",
                "value": str(exc),
                "status": {"ok": False, "detail": "unreadable"},
            }
        )
        return rows
    rows.append({"label": "Journal mode", "value": journal})
    rows.append({"label": "Schema version", "value": ver_row[0] if ver_row else "?"})
    return rows


@router.get("/settings", response_class=HTMLResponse)
async def settings_page() -> HTMLResponse:
    s = get_settings()
    rv = _router_view()

    # --- live probes, run concurrently --------------------------------------
    jira_url = f"{s.jira_base_url.rstrip('/')}/rest/api/{s.jira_api_version}/health"
    gitlab_url = f"{s.gitlab_base_url.rstrip('/')}/users/sign_in"
    ollama_url = f"{s.ollama_base_url.rstrip('/')}/models"
    otlp_url = s.otlp_endpoint
    mm_url = f"{s.mattermost_url.rstrip('/')}/api/v4/system/ping"

    probe_keys = ["jira", "gitlab", "ollama", "otlp"]
    coros = [_probe(jira_url), _probe(gitlab_url), _probe(ollama_url), _probe(otlp_url)]
    if s.chat_provider == "mattermost":
        probe_keys.append("mattermost")
        coros.append(_probe(mm_url))
    results = dict(zip(probe_keys, await asyncio.gather(*coros), strict=True))

    anthropic_set = bool(s.anthropic_api_key)

    # --- sections -----------------------------------------------------------
    llm_rows: list[dict[str, Any]] = [
        {"label": "Default provider", "value": s.default_provider},
        {"label": "Router config", "value": rv["config_path"]},
        {"label": "Anthropic API key", "value": _mask(s.anthropic_api_key)},
        {"label": "Ollama base URL", "value": s.ollama_base_url, "status": results["ollama"]},
        {"label": "Ollama default model", "value": s.ollama_model},
    ]
    jira_rows = [
        {"label": "Base URL", "value": s.jira_base_url, "status": results["jira"]},
        {"label": "API version", "value": s.jira_api_version},
        {"label": "Token", "value": _mask(s.jira_token)},
        {"label": "Points field", "value": s.jira_points_field},
    ]
    gitlab_rows = [
        {"label": "Base URL", "value": s.gitlab_base_url, "status": results["gitlab"]},
        {"label": "Token", "value": _mask(s.gitlab_token)},
    ]
    if s.chat_provider == "mattermost":
        chat_rows = [
            {"label": "Provider", "value": "mattermost"},
            {"label": "URL", "value": s.mattermost_url, "status": results["mattermost"]},
            {"label": "Team", "value": s.mattermost_team},
            {"label": "Token", "value": _mask(s.mattermost_token)},
        ]
    else:
        chat_rows = [
            {"label": "Provider", "value": "slack"},
            {"label": "Bot token", "value": _mask(s.slack_bot_token)},
            {"label": "Signing secret", "value": _mask(s.slack_signing_secret)},
        ]
    obs_rows = [
        {"label": "OTLP endpoint", "value": s.otlp_endpoint, "status": results["otlp"]},
        {"label": "JSONL sink", "value": "on" if s.enable_jsonl_sink else "off"},
        {"label": "Log LLM payloads", "value": "on" if s.log_llm_payloads else "off"},
    ]

    sections = [
        {"title": "LLM & router", "icon": "✦", "rows": llm_rows},
        {"title": "Jira", "icon": "▤", "rows": jira_rows},
        {"title": "GitLab", "icon": "⎇", "rows": gitlab_rows},
        {"title": "Chat", "icon": "✎", "rows": chat_rows},
        {"title": "Observability", "icon": "◉", "rows": obs_rows},
        {"title": "Storage", "icon": "▦", "rows": _storage_rows(s)},
    ]

    # --- top-of-page warnings: backends the router actually relies on --------
    warnings: list[str] = []
    if "ollama" in rv["used_providers"] and not results["ollama"]["ok"]:
        warnings.append(
            f"The active router ({rv['config_path']}) routes phases to Ollama, but "
            f"{s.ollama_base_url} is unreachable ({results['ollama']['detail']}). "
            "Start the `local` profile (ollama) or switch TLA_ROUTER_CONFIG to the "
            "Anthropic router."
        )
    if "anthropic" in rv["used_providers"] and not anthropic_set:
        warnings.append(
            "The active router uses Anthropic, but no API key is set (TLA_ANTHROPIC_API_KEY)."
        )

    template = _env.get_template("settings.html")
    return HTMLResponse(
        template.render(sections=sections, router_routes=rv["routes"], warnings=warnings)
    )

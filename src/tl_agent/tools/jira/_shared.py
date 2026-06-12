"""HTTP client, Jira API surface helpers, and pagination shared by the Jira tools.

Two Jira surfaces are in play:
- **Core REST** (`/rest/api/{2,3}`) for issues, changelog, comments. v3 is
  Jira Cloud (ADF comment bodies); v2 is Server/Data Center (plain text).
  Only the version prefix + comment serialization differ — see `api_base`
  and `comment_payload`. The version comes from `settings.jira_api_version`.
- **Agile API** (`/rest/agile/1.0`) for boards + sprints. This surface is
  version-independent (orthogonal to v2/v3).
"""

from __future__ import annotations

from typing import Any

import httpx

from tl_agent.settings import get_settings
from tl_agent.tools._http import http_client, raise_from_http_error, raise_from_transport_error


def jira_client() -> httpx.AsyncClient:
    s = get_settings()
    return http_client(
        base_url=s.jira_base_url,
        headers={"Authorization": f"Bearer {s.jira_token}", "Accept": "application/json"},
    )


def api_base() -> str:
    """Core REST prefix for the configured Jira flavor (`/rest/api/2|3`)."""
    return f"/rest/api/{get_settings().jira_api_version}"


def comment_payload(body: str) -> dict[str, Any]:
    """v3 (Cloud) requires ADF; v2 (Server/DC) takes a plain string."""
    if get_settings().jira_api_version == "2":
        return {"body": body}
    return {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": body}]}],
        }
    }


async def paginate(
    client: httpx.AsyncClient,
    path: str,
    items_key: str,
    tool_label: str,
    *,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Walk a Jira paginated list (`startAt`/`maxResults`/`total`/`isLast`)."""
    items: list[dict[str, Any]] = []
    start_at = 0
    while True:
        query: dict[str, Any] = {"startAt": start_at}
        if params:
            query.update(params)
        try:
            r = await client.get(path, params=query)
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise_from_http_error(exc, tool_label=tool_label)
        except httpx.HTTPError as exc:
            raise_from_transport_error(exc, tool_label=tool_label)
        data: dict[str, Any] = r.json()
        page: list[dict[str, Any]] = data.get(items_key) or []
        items.extend(page)
        is_last = data.get("isLast")
        total = data.get("total")
        max_results = data.get("maxResults")
        if is_last is True or not page:
            break
        if total is not None:
            if len(items) >= int(total):
                break
        elif max_results is not None:
            if len(page) < int(max_results):
                break
        elif is_last is None:
            # No pagination metadata at all → treat as a single page rather
            # than loop forever on a server that returns a bare list.
            break
        start_at += len(page)
    return items

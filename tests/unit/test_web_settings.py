"""Settings/diagnostics page: renders config + probes, warns on a router that
depends on an unreachable backend."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tl_agent.storage import connect, initialize
from tl_agent.web.routes import settings as settings_route


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    db_path = tmp_path / "settings.db"
    monkeypatch.setenv("TLA_SQLITE_PATH", str(db_path))
    conn = connect(db_path)
    initialize(conn)
    conn.close()

    # Keep the page offline + deterministic — no real network probes.
    async def _fake_probe(url: str, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
        return {"ok": False, "detail": "ConnectError", "url": url}

    monkeypatch.setattr(settings_route, "_probe", _fake_probe)

    from tl_agent.web.app import app

    yield TestClient(app)


def test_settings_page_renders_sections_and_routes(client: TestClient) -> None:
    r = client.get("/settings")
    assert r.status_code == 200
    for title in ("LLM &amp; router", "Jira", "GitLab", "Chat", "Observability", "Storage"):
        assert title in r.text
    # The model-route map is the key diagnostic for "what runs where".
    assert "Active model routes" in r.text
    assert "standup_segments" in r.text
    # Secrets are masked, never echoed verbatim.
    assert "not set" in r.text or "set ·" in r.text


def test_settings_warns_when_router_provider_unreachable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Point the router at the Ollama config; with the probe down, the page must
    # call out that the active router depends on an unreachable backend.
    monkeypatch.setenv("TLA_ROUTER_CONFIG", "config/router.ollama.yaml")
    r = client.get("/settings")
    assert r.status_code == 200
    assert "routes phases to Ollama" in r.text

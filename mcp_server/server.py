"""MCP server — exposes tl-agent's eval + trace machinery as MCP tools.

Lets Claude Code drive the same iteration loop a human would: list hot
spots, look up a trace, run an eval case, replay against a new prompt.

Surface (kept small on purpose):
- run_evals(suite)              — runs the quick/full suite, returns the report
- get_trace(date)               — returns the JSONL trace for a date
- list_hotspots(date)           — returns Phase 7 decisions for a date

Run with:  uv run python -m mcp_server.server

NOTE: this module imports the optional `mcp` SDK, an extras dep. Run
`uv sync --extra mcp` before launching.
"""

# ruff: noqa
# type: ignore

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _read_trace(target: str) -> list[dict[str, Any]]:
    p = Path("traces") / target / "spans.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def _list_hotspots(target: str) -> list[dict[str, Any]]:
    from tl_agent.settings import get_settings
    from tl_agent.storage import connect
    from tl_agent.storage.repos import decisions as decisions_repo

    conn = connect(get_settings().sqlite_path)
    rows = decisions_repo.list_recent(conn, limit=200)
    return [r.model_dump(mode="json") for r in rows if r.created_at.date().isoformat() == target]


async def _run_evals(suite: str) -> dict[str, Any]:
    from evals.runner import render_report, run_suite

    result = await run_suite(suite)
    return {
        "total": result.total,
        "passed": result.passed,
        "rate": result.overall_rate,
        "report_markdown": render_report(result),
    }


async def main() -> None:
    try:
        await _run_real_mcp()
    except ImportError:
        logger.warning(
            "mcp package not installed; falling back to JSON stdio echo. "
            "Install with `uv sync --extra mcp`."
        )
        _run_stdio_echo()


async def _run_real_mcp() -> None:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool

    server = Server("tl-agent")

    @server.list_tools()
    async def list_tools():
        return [
            Tool(
                name="run_evals",
                description="Run the eval suite (quick|full). Returns report.",
                inputSchema={
                    "type": "object",
                    "properties": {"suite": {"type": "string", "enum": ["quick", "full"]}},
                    "required": ["suite"],
                },
            ),
            Tool(
                name="get_trace",
                description="Return the JSONL trace for a date (YYYY-MM-DD).",
                inputSchema={
                    "type": "object",
                    "properties": {"date": {"type": "string"}},
                    "required": ["date"],
                },
            ),
            Tool(
                name="list_hotspots",
                description="Return Phase 7 decisions for a date (YYYY-MM-DD).",
                inputSchema={
                    "type": "object",
                    "properties": {"date": {"type": "string"}},
                    "required": ["date"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]):
        if name == "run_evals":
            result = await _run_evals(arguments["suite"])
            return [TextContent(type="text", text=json.dumps(result))]
        if name == "get_trace":
            return [TextContent(type="text", text=json.dumps(_read_trace(arguments["date"])))]
        if name == "list_hotspots":
            return [TextContent(type="text", text=json.dumps(_list_hotspots(arguments["date"])))]
        return [TextContent(type="text", text=json.dumps({"error": f"unknown tool: {name}"}))]

    del list_tools, call_tool

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def _run_stdio_echo() -> None:
    sys.stdout.write(
        json.dumps(
            {
                "error": "mcp package not installed",
                "hint": "run `uv sync --extra mcp` and re-launch",
                "today": date.today().isoformat(),
            }
        )
        + "\n"
    )
    sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(main())

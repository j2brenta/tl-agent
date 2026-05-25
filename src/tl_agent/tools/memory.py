"""Tools that read from our own SQLite (LAYER 2) — Phase 5 deep-dive helpers.

These read-only tools let the agent search standup history (FTS5) and fetch
engineer baselines. Different from the Jira/GitLab tools in that they hit
our local SQLite rather than an external service.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from typing import ClassVar

from pydantic import BaseModel, Field

from tl_agent.storage.repos import baselines, observations
from tl_agent.tools.base import BaseTool, ToolErrorKind, ToolException
from tl_agent.tools.registry import registry

# We need a connection accessor that the orchestrator wires up per-run.
# Module-level callable; phases set it; tools call it.
_CONN_GETTER: list[object] = []


def set_sqlite_connection(getter: object) -> None:
    """Inject the connection-getter callable. Called once per run by the orchestrator."""
    _CONN_GETTER.clear()
    _CONN_GETTER.append(getter)


def _conn() -> sqlite3.Connection:
    if not _CONN_GETTER:
        raise ToolException(
            kind=ToolErrorKind.UNKNOWN,
            message=(
                "memory tools: SQLite connection not configured. "
                "Call set_sqlite_connection() before use."
            ),
        )
    getter = _CONN_GETTER[0]
    return getter()  # type: ignore[no-any-return,operator]


# -------------------- search_standup_history --------------------


class SearchStandupIn(BaseModel):
    query: str = Field(min_length=1, description="FTS5 query, e.g. 'publisher AND retry'")
    engineer_id: str | None = None
    days: int | None = Field(default=None, ge=1, le=90)
    today_iso: str | None = Field(default=None, description="ISO date for 'days' window anchor")
    limit: int = Field(default=20, ge=1, le=100)


class SearchHit(BaseModel):
    run_date: str
    engineer_id: str
    raw: str
    summary: str | None = None
    rank: float


class SearchStandupOut(BaseModel):
    hits: list[SearchHit] = Field(default_factory=list[SearchHit])


class SearchStandupHistoryTool(BaseTool[SearchStandupIn, SearchStandupOut]):
    name: ClassVar[str] = "search_standup_history"
    description: ClassVar[str] = (
        "Full-text search over recorded standup observations. Returns ranked "
        "hits with engineer, date, raw text, and summary. Use FTS5 syntax: "
        "'word AND word', '\"exact phrase\"', 'NOT word'."
    )
    input_model: ClassVar[type[BaseModel]] = SearchStandupIn
    output_model: ClassVar[type[BaseModel]] = SearchStandupOut

    async def _call(self, args: SearchStandupIn) -> SearchStandupOut:
        today = date.fromisoformat(args.today_iso) if args.today_iso else None
        rows = observations.search(
            _conn(),
            query=_sanitize_fts_query(args.query),
            engineer_id=args.engineer_id,
            days=args.days,
            today=today,
            limit=args.limit,
        )
        hits = [
            SearchHit(
                run_date=r.run_date.isoformat(),
                engineer_id=r.engineer_id,
                raw=r.raw,
                summary=r.summary,
                rank=r.rank,
            )
            for r in rows
        ]
        return SearchStandupOut(hits=hits)


_FTS_OPERATORS = {"AND", "OR", "NOT", "NEAR"}
_FTS_WORD_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _sanitize_fts_query(query: str) -> str:
    """Quote bare terms that contain FTS5-special characters.

    FTS5 parses `ENG-9` as `ENG MINUS 9` and bombs with `no such column: 9`.
    Anything containing characters outside `[A-Za-z0-9_]` must be wrapped in
    double quotes. Operators and parens pass through untouched so callers can
    still write `AND` / `OR` / `NOT` / `NEAR`.

    If the input already contains a `"`, we assume the caller knows FTS5
    syntax and pass it through verbatim — naive token-splitting would break
    multi-word quoted phrases.
    """
    if '"' in query:
        return query
    out: list[str] = []
    for raw in query.split():
        stripped = raw.strip("()")
        leading = raw[: len(raw) - len(raw.lstrip("("))]
        trailing = raw[len(raw.rstrip(")")) :]
        if not stripped or stripped.upper() in _FTS_OPERATORS or _FTS_WORD_RE.match(stripped):
            out.append(raw)
            continue
        out.append(f'{leading}"{stripped}"{trailing}')
    return " ".join(out)


# -------------------- get_baseline --------------------


class GetBaselineIn(BaseModel):
    engineer_id: str = Field(min_length=1)
    window: str = Field(default="7d", pattern=r"^\d+d$")
    metric: str = Field(min_length=1)


class BaselineValue(BaseModel):
    value: float
    window: str
    metric: str


class GetBaselineOut(BaseModel):
    engineer_id: str
    baseline: BaselineValue | None = None  # None if not yet computed


class GetBaselineTool(BaseTool[GetBaselineIn, GetBaselineOut]):
    name: ClassVar[str] = "get_baseline"
    description: ClassVar[str] = (
        "Return one rolling baseline metric for an engineer (e.g. "
        "standup_line_count_avg over 7d). Returns null if not yet computed."
    )
    input_model: ClassVar[type[BaseModel]] = GetBaselineIn
    output_model: ClassVar[type[BaseModel]] = GetBaselineOut

    async def _call(self, args: GetBaselineIn) -> GetBaselineOut:
        b = baselines.get(
            _conn(), engineer_id=args.engineer_id, window=args.window, metric=args.metric
        )
        if not b:
            return GetBaselineOut(engineer_id=args.engineer_id, baseline=None)
        return GetBaselineOut(
            engineer_id=args.engineer_id,
            baseline=BaselineValue(value=b.value, window=b.window, metric=b.metric),
        )


# -------------------- register --------------------


def register_memory_tools() -> None:
    import contextlib

    from tl_agent.tools.registry import RegistryError

    for tool_cls in (SearchStandupHistoryTool, GetBaselineTool):
        with contextlib.suppress(RegistryError):
            registry.register(tool_cls())

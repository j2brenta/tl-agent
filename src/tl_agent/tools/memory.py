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
    """Quote bare terms containing FTS5-special characters.

    FTS5 parses `ENG-9` as `ENG MINUS 9` and bombs with `no such column: 9`.
    Anything containing characters outside `[A-Za-z0-9_]` must be wrapped in
    double quotes. Operators (AND/OR/NOT/NEAR) and parens pass through.

    Quote-aware: the LLM routinely mixes quoted phrases with bare hyphenated
    ticket keys in one query (e.g. `ENG-19 OR "billing dashboard"`). We walk
    the string instead of a blanket split so existing `"..."` phrases pass
    through untouched while bare `ENG-19` between them gets quoted.
    """
    out: list[str] = []
    i = 0
    n = len(query)
    while i < n:
        ch = query[i]
        if ch.isspace():
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            # copy the whole quoted phrase, including the closing quote
            j = query.find('"', i + 1)
            if j == -1:
                # unterminated quote — treat the rest as one phrase
                out.append(query[i:])
                break
            out.append(query[i : j + 1])
            i = j + 1
            continue
        if ch in "()":
            out.append(ch)
            i += 1
            continue
        # bare token — read until whitespace, paren, or quote
        j = i
        while j < n and not query[j].isspace() and query[j] not in '()"':
            j += 1
        token = query[i:j]
        i = j
        if token.upper() in _FTS_OPERATORS or _FTS_WORD_RE.match(token):
            out.append(token)
        else:
            out.append(f'"{token}"')
    return "".join(out)


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

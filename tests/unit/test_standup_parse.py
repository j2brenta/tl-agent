"""`parse_segments` — segmentation/classification with the SQLite cache.

The core guarantee under test: a message already segmented (by any caller —
Workflow's "Collect Standup" button, the Sprint page's "Import from
Mattermost", or a pipeline run) is never re-sent to the LLM. We don't mock
the database (per CLAUDE.md) — only the LLM provider.
"""

from __future__ import annotations

import sqlite3

import pytest
from pydantic import BaseModel

from tl_agent.llm.base import CompletionRequest, CompletionResponse, Provider, TokenUsage
from tl_agent.llm.router import ModelRoute, Router, RouterConfig
from tl_agent.models.signals import StandupMessage, StandupSegmentKind
from tl_agent.phases.standup_parse import _SegmentsOut, parse_segments
from tl_agent.storage import connect, initialize
from tl_agent.storage.repos import standup_segments as segments_repo


class FakeProvider(Provider):
    """Returns a canned `_SegmentsOut`; counts how many times it's called."""

    name = "fake"

    def __init__(self, result: _SegmentsOut) -> None:
        self.result = result
        self.calls = 0

    async def complete(self, req: CompletionRequest) -> CompletionResponse:
        raise NotImplementedError

    async def structured[T: BaseModel](
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: type[T],
        max_tokens: int = 1024,
        temperature: float = 0.0,
        cache_system: bool = False,
        phase: str | None = None,
    ) -> tuple[T, TokenUsage]:
        self.calls += 1
        assert schema is _SegmentsOut
        usage = TokenUsage(input_tokens=10, output_tokens=5)
        return self.result, usage

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4


class FailingProvider(Provider):
    name = "fake"

    async def complete(self, req: CompletionRequest) -> CompletionResponse:
        raise NotImplementedError

    async def structured[T: BaseModel](
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: type[T],
        max_tokens: int = 1024,
        temperature: float = 0.0,
        cache_system: bool = False,
        phase: str | None = None,
    ) -> tuple[T, TokenUsage]:
        raise RuntimeError("LLM unavailable")

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4


def _router(provider: Provider) -> Router:
    config = RouterConfig(
        routes={
            "standup_segments": ModelRoute(
                provider="anthropic",
                model="claude-haiku-4-5",
                max_tokens=512,
                temperature=0.0,
                cache_system=True,
            )
        }
    )
    return Router({"anthropic": provider}, config)


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    initialize(conn)
    return conn


_DEFAULT_TEXT = "Working on ENG-12.\n\nAlso, found a fun Rust article!"


def _message(text: str = _DEFAULT_TEXT) -> StandupMessage:
    return StandupMessage(
        engineer_id="john",
        date_iso="2026-05-22",
        raw=text,
        chat_message_id="m1",
        chat_channel_id="town-square",
    )


def _two_segments() -> _SegmentsOut:
    from tl_agent.phases.standup_parse import _SegmentOut

    return _SegmentsOut(
        segments=[
            _SegmentOut(text="Working on ENG-12.", kind="update"),
            _SegmentOut(text="Also, found a fun Rust article!", kind="off_topic"),
        ]
    )


async def test_first_call_hits_llm_and_persists(db: sqlite3.Connection) -> None:
    provider = FakeProvider(_two_segments())
    router = _router(provider)
    notes: list[str] = []

    segments = await parse_segments(db, router, [_message()], notes=notes)

    assert provider.calls == 1
    expected_kinds = [StandupSegmentKind.UPDATE, StandupSegmentKind.OFF_TOPIC]
    assert [s.kind for s in segments] == expected_kinds
    assert segments[0].text == "Working on ENG-12."
    assert segments[1].text == "Also, found a fun Rust article!"

    # Persisted under the cache key.
    cached = segments_repo.get_for_message(db, chat_message_id="m1", engineer_id="john")
    assert cached == segments


async def test_second_call_reuses_cache_zero_llm_calls(db: sqlite3.Connection) -> None:
    provider = FakeProvider(_two_segments())
    router = _router(provider)
    notes: list[str] = []

    first = await parse_segments(db, router, [_message()], notes=notes)
    assert provider.calls == 1

    second = await parse_segments(db, router, [_message()], notes=notes)
    assert provider.calls == 1  # no new LLM call
    assert second == first


async def test_llm_failure_falls_back_to_single_update_segment(db: sqlite3.Connection) -> None:
    provider = FailingProvider()
    router = _router(provider)
    notes: list[str] = []

    segments = await parse_segments(db, router, [_message("just one line, all good")], notes=notes)

    assert len(segments) == 1
    assert segments[0].kind is StandupSegmentKind.UPDATE
    assert segments[0].text == "just one line, all good"
    assert any("standup_segments" in n for n in notes)

    # Not cached, so a retry can succeed later.
    assert segments_repo.get_for_message(db, chat_message_id="m1", engineer_id="john") == []

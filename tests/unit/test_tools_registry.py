"""Tests for the tool registry, retry/idempotency wrapper, and readback.

We define small synthetic tools (a reader and a writer) to exercise the
contract — the real Jira/GitLab/chat tools are tested under integration.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import ClassVar

import pytest
from pydantic import BaseModel

from tl_agent.storage import connect, initialize
from tl_agent.tools import (
    BaseTool,
    RegistryError,
    RetryPolicy,
    SqliteIdempotencyStore,
    ToolError,
    ToolErrorKind,
    ToolException,
    ToolResult,
    make_key,
    readback,
    registry,
)

# -------------------- fixtures --------------------


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    registry.reset()


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = connect(":memory:")
    initialize(conn)
    return conn


# -------------------- synthetic tools --------------------


class _Add(BaseModel):
    a: int
    b: int


class _Sum(BaseModel):
    total: int


class AddTool(BaseTool[_Add, _Sum]):
    name: ClassVar[str] = "add"
    description: ClassVar[str] = "Add two ints."
    input_model: ClassVar[type[BaseModel]] = _Add
    output_model: ClassVar[type[BaseModel]] = _Sum

    async def _call(self, args: _Add) -> _Sum:
        return _Sum(total=args.a + args.b)


class _Post(BaseModel):
    body: str
    recipient: str


class _Posted(BaseModel):
    message_id: str
    body: str


class PostTool(BaseTool[_Post, _Posted]):
    name: ClassVar[str] = "post_dm"
    description: ClassVar[str] = "Send a DM."
    input_model: ClassVar[type[BaseModel]] = _Post
    output_model: ClassVar[type[BaseModel]] = _Posted
    requires_approval: ClassVar[bool] = True
    retry_policy: ClassVar[RetryPolicy] = RetryPolicy(max_attempts=2, initial_delay_ms=1)

    call_count = 0

    def __init__(self) -> None:
        type(self).call_count = 0

    async def _call(self, args: _Post) -> _Posted:
        type(self).call_count += 1
        return _Posted(message_id=f"m-{type(self).call_count}", body=args.body)

    def idempotency_key(self, args: _Post, *, run_date_iso: str) -> str | None:
        return make_key(tool_name=self.name, args=args, run_date_iso=run_date_iso)


class FlakyTool(BaseTool[_Add, _Sum]):
    name: ClassVar[str] = "flaky_add"
    description: ClassVar[str] = "Fails then succeeds."
    input_model: ClassVar[type[BaseModel]] = _Add
    output_model: ClassVar[type[BaseModel]] = _Sum
    retry_policy: ClassVar[RetryPolicy] = RetryPolicy(max_attempts=3, initial_delay_ms=1)

    attempts_seen = 0

    def __init__(self) -> None:
        type(self).attempts_seen = 0

    async def _call(self, args: _Add) -> _Sum:
        type(self).attempts_seen += 1
        if type(self).attempts_seen < 3:
            raise ToolException(ToolErrorKind.UPSTREAM_5XX, "transient")
        return _Sum(total=args.a + args.b)


# -------------------- registry --------------------


def test_register_and_get() -> None:
    add = AddTool()
    registry.register(add)
    assert registry.get("add") is add
    assert registry.all() == [add]


def test_collision_raises() -> None:
    registry.register(AddTool())
    with pytest.raises(RegistryError):
        registry.register(AddTool())  # different instance, same name


def test_for_phase5_excludes_writers() -> None:
    registry.register(AddTool())
    registry.register(PostTool())
    bound = registry.for_phase5()
    names = [t.name for t in bound]
    assert "add" in names
    assert "post_dm" not in names


def test_for_phase5_refuses_writer_in_allow_list() -> None:
    registry.register(AddTool())
    registry.register(PostTool())
    with pytest.raises(RegistryError) as exc:
        registry.for_phase5(allowed=["add", "post_dm"])
    assert "post_dm" in str(exc.value)


def test_anthropic_schemas_emitted() -> None:
    registry.register(AddTool())
    schemas = registry.anthropic_schemas()
    assert len(schemas) == 1
    schema = schemas[0]
    assert schema["name"] == "add"
    assert schema["description"] == "Add two ints."
    assert "properties" in schema["input_schema"]
    assert "a" in schema["input_schema"]["properties"]


def test_name_must_be_snake_case_alnum() -> None:
    class BadName(AddTool):
        name: ClassVar[str] = "bad-name!"

    with pytest.raises(RegistryError):
        registry.register(BadName())


# -------------------- invoke wrapper --------------------


async def test_invoke_validates_input() -> None:
    tool = AddTool()
    result = await tool.invoke({"a": "not-an-int", "b": 2}, run_date_iso="2026-05-22")
    assert isinstance(result, ToolError)
    assert result.kind is ToolErrorKind.VALIDATION


async def test_invoke_round_trip_success() -> None:
    tool = AddTool()
    result = await tool.invoke({"a": 2, "b": 3}, run_date_iso="2026-05-22")
    assert isinstance(result, ToolResult)
    assert result.value.total == 5
    assert not result.cached
    assert result.latency_ms >= 0


async def test_invoke_retries_on_5xx_until_success() -> None:
    tool = FlakyTool()
    result = await tool.invoke({"a": 1, "b": 1}, run_date_iso="2026-05-22")
    assert isinstance(result, ToolResult)
    assert result.value.total == 2
    assert FlakyTool.attempts_seen == 3


async def test_invoke_gives_up_after_max_attempts() -> None:
    class AlwaysFails(BaseTool[_Add, _Sum]):
        name: ClassVar[str] = "always_fails"
        description: ClassVar[str] = "Never succeeds."
        input_model: ClassVar[type[BaseModel]] = _Add
        output_model: ClassVar[type[BaseModel]] = _Sum
        retry_policy: ClassVar[RetryPolicy] = RetryPolicy(max_attempts=2, initial_delay_ms=1)

        async def _call(self, args: _Add) -> _Sum:
            raise ToolException(ToolErrorKind.UPSTREAM_5XX, "down")

    tool = AlwaysFails()
    result = await tool.invoke({"a": 1, "b": 1}, run_date_iso="2026-05-22")
    assert isinstance(result, ToolError)
    assert result.kind is ToolErrorKind.UPSTREAM_5XX


async def test_invoke_validation_error_not_retried() -> None:
    class BadValidation(BaseTool[_Add, _Sum]):
        name: ClassVar[str] = "bad_validation"
        description: ClassVar[str] = "Throws validation error."
        input_model: ClassVar[type[BaseModel]] = _Add
        output_model: ClassVar[type[BaseModel]] = _Sum
        retry_policy: ClassVar[RetryPolicy] = RetryPolicy(max_attempts=5, initial_delay_ms=1)
        attempts = 0

        async def _call(self, args: _Add) -> _Sum:
            type(self).attempts += 1
            raise ToolException(ToolErrorKind.VALIDATION, "bad input")

    tool = BadValidation()
    await tool.invoke({"a": 1, "b": 1}, run_date_iso="2026-05-22")
    assert BadValidation.attempts == 1  # not retried


# -------------------- idempotency --------------------


async def test_idempotency_short_circuits_second_call(db: sqlite3.Connection) -> None:
    store = SqliteIdempotencyStore(db)
    tool = PostTool()
    r1 = await tool.invoke(
        {"body": "hi", "recipient": "john"},
        run_date_iso="2026-05-22",
        idempotency_lookup=store,
    )
    r2 = await tool.invoke(
        {"body": "hi", "recipient": "john"},
        run_date_iso="2026-05-22",
        idempotency_lookup=store,
    )
    assert isinstance(r1, ToolResult)
    assert isinstance(r2, ToolResult)
    assert not r1.cached
    assert r2.cached
    assert r1.value.message_id == r2.value.message_id  # same id served from cache
    assert PostTool.call_count == 1  # _call invoked only once


async def test_idempotency_distinct_per_run_date(db: sqlite3.Connection) -> None:
    store = SqliteIdempotencyStore(db)
    tool = PostTool()
    args = {"body": "hi", "recipient": "john"}
    await tool.invoke(args, run_date_iso="2026-05-22", idempotency_lookup=store)
    # different run date ⇒ different key ⇒ should NOT short-circuit
    await tool.invoke(args, run_date_iso="2026-05-23", idempotency_lookup=store)
    assert PostTool.call_count == 2


# -------------------- readback --------------------


async def test_readback_pass() -> None:
    target = {"id": "m-1", "body": "hello"}

    async def fetch() -> dict[str, str]:
        return target

    result = await readback(
        fetch=fetch,
        matches=lambda fetched: fetched["body"] == "hello",
        label="post_dm",
    )
    assert result.fetched is target


async def test_readback_missing_raises_retriable() -> None:
    async def fetch() -> None:
        return None

    with pytest.raises(ToolException) as exc:
        await readback(fetch=fetch, matches=lambda _x: True, label="post_dm")
    assert exc.value.kind is ToolErrorKind.UNKNOWN
    assert exc.value.retriable


async def test_readback_mismatch_raises_non_retriable() -> None:
    async def fetch() -> dict[str, str]:
        return {"body": "wrong"}

    with pytest.raises(ToolException) as exc:
        await readback(fetch=fetch, matches=lambda x: x["body"] == "expected", label="post_dm")
    assert not exc.value.retriable


# -------------------- end-to-end smoke (async fixture) --------------------


async def test_invoke_blocks_until_complete() -> None:
    tool = AddTool()
    results = await asyncio.gather(
        tool.invoke({"a": 1, "b": 1}, run_date_iso="2026-05-22"),
        tool.invoke({"a": 2, "b": 2}, run_date_iso="2026-05-22"),
        tool.invoke({"a": 3, "b": 3}, run_date_iso="2026-05-22"),
    )
    totals = [r.value.total for r in results if isinstance(r, ToolResult)]
    assert totals == [2, 4, 6]

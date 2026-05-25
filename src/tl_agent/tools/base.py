"""Tool contract.

Every tool subclasses `BaseTool` and pins:
- Pydantic input/output models
- An idempotency-key function (writers only; readers default to "none")
- A retry policy (exponential backoff over a typed set of retriable errors)
- A `requires_approval` flag — writers default True; the registry
  enforces that Phase 5's ReACT binding cannot include any such tool.

Tools return `ToolResult[OutputT] | ToolError` — never raise across the
boundary. The orchestrator/agent reads the structured result and decides
what to do (retry, give up, escalate).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar, cast

from opentelemetry.trace import Status, StatusCode
from pydantic import BaseModel

from tl_agent.obs.spans import tool_span

logger = logging.getLogger(__name__)


class ToolErrorKind(StrEnum):
    """The categorical reason a tool call failed.

    Each kind maps to a distinct response strategy in `RetryPolicy.should_retry`.
    Keep the set small and exhaustive — the agent prompts reference these.
    """

    VALIDATION = "validation"  # bad args; do NOT retry — fix the call
    NOT_FOUND = "not_found"  # 404; do NOT retry
    UNAUTHORIZED = "unauthorized"  # 401/403; do NOT retry without refresh
    RATE_LIMIT = "rate_limit"  # 429; DO retry with backoff
    UPSTREAM_5XX = "upstream_5xx"  # transient; DO retry
    TIMEOUT = "timeout"  # DO retry once
    CONFLICT = "conflict"  # 409; bubble up
    UNKNOWN = "unknown"  # unclassified; treat as upstream_5xx


@dataclass(frozen=True)
class ToolError:
    """Structured failure — never thrown across the tool boundary."""

    kind: ToolErrorKind
    message: str
    retriable: bool = False
    upstream_status: int | None = None
    detail: dict[str, str] | None = None


@dataclass(frozen=True)
class ToolResult[OutputT: BaseModel]:
    """Success envelope. `cached=True` ⇒ short-circuited via idempotency."""

    value: OutputT
    cached: bool = False
    latency_ms: float = 0.0


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff with jitter over the retriable error kinds."""

    max_attempts: int = 3
    initial_delay_ms: int = 200
    max_delay_ms: int = 5_000
    multiplier: float = 2.0
    retry_on: frozenset[ToolErrorKind] = field(
        default_factory=lambda: frozenset(
            {ToolErrorKind.RATE_LIMIT, ToolErrorKind.UPSTREAM_5XX, ToolErrorKind.TIMEOUT}
        )
    )

    def should_retry(self, err: ToolError, attempt: int) -> bool:
        return attempt < self.max_attempts and (err.retriable or err.kind in self.retry_on)

    def sleep_for(self, attempt: int) -> float:
        """Seconds to wait before attempt N (1-indexed). Includes jitter."""
        delay_ms = min(
            self.initial_delay_ms * (self.multiplier ** (attempt - 1)), self.max_delay_ms
        )
        jitter = delay_ms * 0.2 * random.random()
        return (delay_ms + jitter) / 1000.0


class BaseTool[InputT: BaseModel, OutputT: BaseModel](ABC):
    """The contract every tool implements.

    Subclasses pin class-level `name`, `description`, `input_model`,
    `output_model`, and override `_call`. The wrapper `invoke` enforces:
      - input validation,
      - idempotency cache lookup (writers),
      - retry policy on retriable errors,
      - latency measurement,
      - output validation,
      - structured `ToolResult | ToolError` return.
    """

    name: ClassVar[str]
    description: ClassVar[str]
    input_model: ClassVar[type[BaseModel]]
    output_model: ClassVar[type[BaseModel]]
    requires_approval: ClassVar[bool] = False  # writers should set True
    retry_policy: ClassVar[RetryPolicy] = RetryPolicy()

    @abstractmethod
    async def _call(self, args: InputT) -> OutputT:
        """The actual implementation. Raise ToolException to signal failure.

        Most subclasses translate HTTP/SDK exceptions to ToolException so the
        retry/idempotency wrapper sees a structured error.
        """

    def idempotency_key(self, args: InputT, *, run_date_iso: str) -> str | None:
        """Stable hash of (tool, normalised args, run_date).

        Default: None — readers don't need dedup. Writers override to return
        a non-None key so the idempotency layer can short-circuit re-runs.
        The run_date is passed in so writers can opt to scope their key to
        the run (typical) or not (rare; cross-day dedup).
        """
        del args, run_date_iso  # unused at base level
        return None

    async def invoke(
        self,
        raw_args: object,
        *,
        run_date_iso: str,
        idempotency_lookup: IdempotencyLookup | None = None,
    ) -> ToolResult[OutputT] | ToolError:
        """Validated, retried, idempotent, instrumented invocation."""
        with tool_span(self.name) as span:
            # 1) input validation
            try:
                args = self.input_model.model_validate(raw_args)
            except Exception as exc:
                err = ToolError(
                    kind=ToolErrorKind.VALIDATION,
                    message=f"input validation failed for {self.name}: {exc}",
                )
                logger.warning(
                    "tool.failed tool=%s kind=%s attempt=0 args=%s msg=%s",
                    self.name,
                    err.kind.value,
                    _summarise_raw(raw_args),
                    err.message,
                )
                _record_tool_error(span, exc, err, attempts=0)
                return err

            # 2) idempotency lookup (writers)
            key = self.idempotency_key(args, run_date_iso=run_date_iso)  # type: ignore[arg-type]
            if key and idempotency_lookup is not None:
                cached = await idempotency_lookup.get(key)
                if cached is not None:
                    logger.info("tool.cache_hit", extra={"tool": self.name, "key": key})
                    try:
                        value = self.output_model.model_validate(cached)
                    except Exception as exc:
                        err = ToolError(
                            kind=ToolErrorKind.VALIDATION,
                            message=f"cached value for {self.name} failed validation: {exc}",
                        )
                        _record_tool_error(span, exc, err, attempts=0)
                        return err
                    span.set_attribute("tl_agent.tool.cached", True)
                    span.set_status(Status(StatusCode.OK))
                    return ToolResult(value=cast(OutputT, value), cached=True, latency_ms=0.0)

            # 3) retry loop
            attempt = 0
            start = time.perf_counter()
            while True:
                attempt += 1
                try:
                    value = await self._call(args)  # type: ignore[arg-type]
                except ToolException as ex:
                    err = ex.to_error()
                    if self.retry_policy.should_retry(err, attempt):
                        await asyncio.sleep(self.retry_policy.sleep_for(attempt))
                        continue
                    logger.warning(
                        "tool.failed tool=%s kind=%s attempt=%d args=%s msg=%s",
                        self.name,
                        err.kind.value,
                        attempt,
                        _summarise_args(args),
                        err.message,
                    )
                    _record_tool_error(span, ex, err, attempts=attempt)
                    return err
                except Exception as exc:
                    # Anything not explicitly modeled becomes UNKNOWN — never bare-raise.
                    err = ToolError(
                        kind=ToolErrorKind.UNKNOWN, message=f"{type(exc).__name__}: {exc}"
                    )
                    if self.retry_policy.should_retry(err, attempt):
                        await asyncio.sleep(self.retry_policy.sleep_for(attempt))
                        continue
                    logger.warning(
                        "tool.failed tool=%s kind=%s attempt=%d args=%s msg=%s",
                        self.name,
                        err.kind.value,
                        attempt,
                        _summarise_args(args),
                        err.message,
                    )
                    _record_tool_error(span, exc, err, attempts=attempt)
                    return err

                latency_ms = (time.perf_counter() - start) * 1000

                # 4) output validation (paranoid — subclass might break the contract)
                try:
                    validated = self.output_model.model_validate(value.model_dump())
                except Exception as exc:
                    err = ToolError(
                        kind=ToolErrorKind.VALIDATION,
                        message=f"output validation failed for {self.name}: {exc}",
                    )
                    _record_tool_error(span, exc, err, attempts=attempt)
                    return err

                # 5) idempotency persist (writers)
                if key and idempotency_lookup is not None:
                    await idempotency_lookup.put(
                        key=key,
                        tool_name=self.name,
                        run_date_iso=run_date_iso,
                        result_json=validated.model_dump_json(),
                    )
                span.set_attribute("tl_agent.tool.attempts", attempt)
                span.set_attribute("tl_agent.latency_ms", latency_ms)
                span.set_status(Status(StatusCode.OK))
                return ToolResult(
                    value=cast(OutputT, validated), cached=False, latency_ms=latency_ms
                )


class ToolException(Exception):
    """Internal sentinel — subclasses raise this to signal a structured error.

    The wrapper translates it to `ToolError` so callers never see raw exceptions.
    """

    def __init__(
        self,
        kind: ToolErrorKind,
        message: str,
        *,
        retriable: bool | None = None,
        upstream_status: int | None = None,
        detail: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retriable = retriable if retriable is not None else _default_retriable(kind)
        self.upstream_status = upstream_status
        self.detail = detail

    def to_error(self) -> ToolError:
        return ToolError(
            kind=self.kind,
            message=self.message,
            retriable=self.retriable,
            upstream_status=self.upstream_status,
            detail=self.detail,
        )


def _default_retriable(kind: ToolErrorKind) -> bool:
    return kind in {ToolErrorKind.RATE_LIMIT, ToolErrorKind.UPSTREAM_5XX, ToolErrorKind.TIMEOUT}


_ARGS_MAX_CHARS = 240


def _summarise_args(args: BaseModel) -> str:
    """Compact one-line repr of validated tool args for log lines."""
    try:
        text = args.model_dump_json()
    except Exception:
        text = repr(args)
    return text if len(text) <= _ARGS_MAX_CHARS else text[:_ARGS_MAX_CHARS] + "…"


def _summarise_raw(raw: object) -> str:
    """Compact repr of pre-validation raw args (may not be a BaseModel)."""
    try:
        text = json.dumps(raw, default=str, sort_keys=True)
    except Exception:
        text = repr(raw)
    return text if len(text) <= _ARGS_MAX_CHARS else text[:_ARGS_MAX_CHARS] + "…"


def _record_tool_error(span: object, exc: BaseException, err: ToolError, *, attempts: int) -> None:
    # OpenTelemetry's Span object is structurally typed here to avoid an
    # import cycle through tl_agent.obs — every span returned by tool_span()
    # implements record_exception / set_status / set_attribute.
    span.record_exception(exc)  # type: ignore[attr-defined]
    span.set_status(Status(StatusCode.ERROR, err.message))  # type: ignore[attr-defined]
    span.set_attribute("tl_agent.tool.error_kind", err.kind.value)  # type: ignore[attr-defined]
    span.set_attribute("tl_agent.tool.attempts", attempts)  # type: ignore[attr-defined]


class IdempotencyLookup:
    """Interface implemented by `tools.idempotency.SqliteIdempotencyStore`.

    Kept as a Protocol-shaped abstract base so `tools.base` doesn't import the
    storage module (avoids a circular dep and lets tests pass a fake).
    """

    async def get(self, key: str) -> object | None:
        raise NotImplementedError

    async def put(self, *, key: str, tool_name: str, run_date_iso: str, result_json: str) -> None:
        raise NotImplementedError


def stable_hash(parts: list[str]) -> str:
    """Sha-256 hex of the joined parts. For idempotency keys."""
    blob = "\x00".join(parts).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def normalize_args(args: BaseModel) -> str:
    """Canonical JSON of `args` so logically-equal inputs hash the same."""
    return json.dumps(args.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

"""Shared HTTP client helper for upstream-API tools.

Centralised so:
- Every tool uses the same connection pool + timeout defaults.
- We can swap httpx for httpcore or instrument it via OTel
  (`opentelemetry-instrumentation-httpx` is already a dep).
- ToolException translation for HTTP failures lives in one place.
"""

from __future__ import annotations

from typing import NoReturn

import httpx

from tl_agent.tools.base import ToolErrorKind, ToolException


def http_client(
    *, base_url: str, headers: dict[str, str], timeout_seconds: float = 10.0
) -> httpx.AsyncClient:
    """Construct a configured httpx AsyncClient."""
    return httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout_seconds)


def raise_from_http_error(exc: httpx.HTTPStatusError, *, tool_label: str) -> NoReturn:
    """Translate an httpx 4xx/5xx into a typed ToolException — never raw."""
    status = exc.response.status_code
    body = exc.response.text[:300]
    kind = _kind_for_status(status)
    raise ToolException(
        kind=kind,
        message=f"{tool_label}: HTTP {status}: {body}",
        upstream_status=status,
    ) from exc


def raise_from_transport_error(exc: httpx.HTTPError, *, tool_label: str) -> NoReturn:
    """Translate a connection/timeout error into a retriable ToolException.

    httpx timeout exceptions (`ConnectTimeout`, `ReadTimeout`, `PoolTimeout`)
    often stringify to "" — include the exception type so notes/logs can
    distinguish "never connected" from "connected but the response never
    arrived" from "queued behind other requests on the same client".
    """
    raise ToolException(
        kind=ToolErrorKind.TIMEOUT,
        message=f"{tool_label}: transport error ({type(exc).__name__}): {exc}",
        retriable=True,
    ) from exc


def _kind_for_status(status: int) -> ToolErrorKind:
    if status == 404:
        return ToolErrorKind.NOT_FOUND
    if status in {401, 403}:
        return ToolErrorKind.UNAUTHORIZED
    if status == 409:
        return ToolErrorKind.CONFLICT
    if status == 429:
        return ToolErrorKind.RATE_LIMIT
    if 500 <= status < 600:
        return ToolErrorKind.UPSTREAM_5XX
    if 400 <= status < 500:
        return ToolErrorKind.VALIDATION
    return ToolErrorKind.UNKNOWN

"""Writer readback — send → fetch → verify it landed.

Pattern: after a write tool succeeds, immediately fetch the object back via
the same provider's read API and assert the body matches what we asked for.
If it doesn't, raise — the cached idempotency entry will NOT be written
(since we surface the error), so a retry can try again.

This is the senior-signal "loop closure on actions" pattern called out in the
plan: "send → read back → confirm landed."
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from tl_agent.tools.base import ToolErrorKind, ToolException


@dataclass(frozen=True)
class ReadbackResult[T]:
    """What `readback` returns when verification passes."""

    fetched: T


async def readback[T](
    *,
    fetch: Callable[[], Awaitable[T | None]],
    matches: Callable[[T], bool],
    label: str,
) -> ReadbackResult[T]:
    """Generic verifier.

    Args:
        fetch:   callable that re-reads the just-written object from upstream.
        matches: predicate over the fetched object — typically compares body
                 + recipient to what we asked for.
        label:   human-readable description for the error message.

    Raises:
        ToolException with kind=UNKNOWN if `fetch` returns None or if
        `matches` is False — both indicate the write didn't actually land.
    """
    fetched = await fetch()
    if fetched is None:
        raise ToolException(
            kind=ToolErrorKind.UNKNOWN,
            message=f"readback for {label}: upstream did not return the written object",
            retriable=True,
        )
    if not matches(fetched):
        raise ToolException(
            kind=ToolErrorKind.UNKNOWN,
            message=f"readback for {label}: fetched object did not match write",
            retriable=False,
        )
    return ReadbackResult(fetched=fetched)

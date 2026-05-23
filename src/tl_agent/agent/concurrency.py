"""Per-provider concurrency control + fan-out helper.

Why this exists: Phase 2 triages four engineers in parallel, Phase 5 runs N
hotspot deep-dives concurrently. Without a per-provider semaphore, we either
hammer one provider and get 429'd, or we serialise unnecessarily.

The orchestrator owns one ConcurrencyGate per provider name and passes it
to anything that calls that provider.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable


class ConcurrencyGate:
    """A named asyncio semaphore. Use as `async with gate:`.

    Per-provider concurrency cap. Defaults are conservative — bump in config
    if you want more parallelism and your rate limits allow.
    """

    def __init__(self, *, name: str, max_concurrent: int = 4) -> None:
        self.name = name
        self._sem = asyncio.Semaphore(max_concurrent)

    async def __aenter__(self) -> ConcurrencyGate:
        await self._sem.acquire()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._sem.release()


async def fan_out[T, R](
    items: Iterable[T],
    *,
    worker: Callable[[T], Awaitable[R]],
    gate: ConcurrencyGate | None = None,
) -> list[R]:
    """Run `worker(item)` for each item concurrently; return results in order.

    If `gate` is provided, each worker is wrapped in `async with gate:` so
    the per-provider cap is respected.
    """

    async def _run(item: T) -> R:
        if gate is None:
            return await worker(item)
        async with gate:
            return await worker(item)

    return await asyncio.gather(*(_run(i) for i in items))

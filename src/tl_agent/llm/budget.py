"""Per-run token/cost budget tracker.

Single-process counter; the orchestrator instantiates one per run and passes
it into every provider call. The router consults it before each call and
raises `BudgetExceeded` if the call would push us over.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock

from tl_agent.llm.base import TokenUsage


class BudgetExceeded(RuntimeError):
    """Raised when a call would exceed the run-level token budget."""


@dataclass
class BudgetTracker:
    """Accumulator for one run.

    Thread-safe via a simple Lock so async tasks fanning out tool calls don't
    race on the counter. `spend()` records actual usage after a call;
    `would_overspend()` is the pre-call check.
    """

    token_cap: int = 500_000
    spent_input: int = 0
    spent_output: int = 0
    spent_usd: float = 0.0
    n_calls: int = 0
    _lock: Lock = field(default_factory=Lock, repr=False)

    @property
    def spent_total_tokens(self) -> int:
        return self.spent_input + self.spent_output

    def would_overspend(self, projected_max: int) -> bool:
        return self.spent_total_tokens + projected_max > self.token_cap

    def check(self, projected_max: int) -> None:
        """Raise if `projected_max` more tokens would blow the cap."""
        if self.would_overspend(projected_max):
            raise BudgetExceeded(
                f"run budget exceeded: {self.spent_total_tokens} + projected {projected_max} "
                f"> cap {self.token_cap}"
            )

    def spend(self, usage: TokenUsage) -> None:
        """Record actual usage from a completed call."""
        with self._lock:
            self.spent_input += usage.input_tokens
            self.spent_output += usage.output_tokens
            self.spent_usd += usage.cost_usd
            self.n_calls += 1

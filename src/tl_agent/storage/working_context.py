"""In-process working context with token-budget compaction.

This is the LLM-visible context buffer for the ReACT loop (Phase 5). It is
distinct from `DurableState` (SQLite) and `References` (IDs/paths fetched on
demand). Holding all three apart is the senior-signal "memory tiers" pattern.

Compaction strategy: when token estimate exceeds `budget`, fold all but the
last `keep_recent` turns into a single summary message. The summary is
produced by a callback the orchestrator wires in (we don't import the LLM
provider here to keep this module pure).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class Turn:
    """One message in the context buffer."""

    role: Role
    content: str
    name: str | None = None  # tool name for role="tool"
    token_estimate: int = 0  # populated when added


@dataclass
class WorkingContext:
    """Bounded conversation buffer.

    `budget` is a soft cap; on overflow `compact()` is invoked. We never drop
    messages silently — always summarise first.
    """

    budget: int = 30_000
    keep_recent: int = 6
    turns: list[Turn] = field(default_factory=list[Turn])

    def add(self, role: Role, content: str, *, name: str | None = None) -> None:
        """Append one turn; caller supplies a token estimate via `estimate()`."""
        self.turns.append(
            Turn(role=role, content=content, name=name, token_estimate=estimate(content))
        )

    def total_tokens(self) -> int:
        return sum(t.token_estimate for t in self.turns)

    def needs_compaction(self) -> bool:
        return self.total_tokens() > self.budget

    def compact(self, summarise: Callable[[list[Turn]], str]) -> None:
        """Fold all but the last `keep_recent` turns into one summary message.

        `summarise` is supplied by the caller (typically a Sonnet call) so the
        module stays decoupled from any LLM provider.
        """
        if len(self.turns) <= self.keep_recent:
            return
        to_fold = self.turns[: -self.keep_recent]
        recent = self.turns[-self.keep_recent :]
        summary_text = summarise(to_fold)
        self.turns = [
            Turn(
                role="system",
                content=f"[context-so-far summary]\n{summary_text}",
                token_estimate=estimate(summary_text),
            ),
            *recent,
        ]


def estimate(text: str) -> int:
    """Crude token estimate: 4 chars/token.

    Good enough for budget decisions; the actual count comes from the
    provider's tokenizer when we want exact numbers.
    """
    return max(1, len(text) // 4)

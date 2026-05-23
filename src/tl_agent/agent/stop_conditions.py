"""Stop conditions for the ReACT loop.

The control flow lives in `loop.py`. This module is the explicit list of
reasons the loop is allowed to terminate. Keeping them in one place + named
makes "why did the loop stop" trivially debuggable in traces.

Reasons (small, exhaustive enum):
  END_TURN           — model returned no tool_use; normal completion
  MAX_STEPS          — exceeded `max_steps` iterations
  WALL_CLOCK         — exceeded `wall_clock_seconds`
  TOKEN_BUDGET       — projected next call would breach `token_budget`
  NO_PROGRESS        — same (tool, args) called consecutively without progress
  REPETITION         — N most recent tool calls are all the same
  VERIFIER_PASSED    — verifier accepted the artifact
  VERIFIER_FAILED    — verifier rejected the artifact (and retries exhausted)
  ERROR              — unrecoverable provider/tool error
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import StrEnum

from tl_agent.llm.base import ToolUseBlock


class StopReason(StrEnum):
    END_TURN = "end_turn"
    MAX_STEPS = "max_steps"
    WALL_CLOCK = "wall_clock"
    TOKEN_BUDGET = "token_budget"  # noqa: S105 — enum string, not a credential
    NO_PROGRESS = "no_progress"
    REPETITION = "repetition"
    VERIFIER_PASSED = "verifier_passed"
    VERIFIER_FAILED = "verifier_failed"
    ERROR = "error"


@dataclass
class StopConditions:
    """Configuration + running state for the loop.

    Stateful by design: the loop calls `check(...)` at every step, and the
    instance accumulates the trace it needs to detect repetition / no-progress.
    Build one per ReACT invocation.
    """

    max_steps: int = 12
    wall_clock_seconds: float = 60.0
    token_budget: int = 30_000
    repetition_window: int = 3  # N identical calls in a row ⇒ REPETITION

    _started_at: float = field(default_factory=time.monotonic, repr=False)
    _step: int = 0
    _tokens_used: int = 0
    _last_tool_hashes: list[str] = field(default_factory=list[str], repr=False)

    def step_start(self) -> None:
        self._step += 1

    @property
    def step(self) -> int:
        return self._step

    def add_tokens(self, tokens: int) -> None:
        self._tokens_used += tokens

    def record_tool_use(self, tool_uses: tuple[ToolUseBlock, ...]) -> None:
        """Record the tool calls the model just emitted (for repetition detection).

        We hash each call as (name, sorted JSON args) so logically-equal calls
        collapse to the same hash. Stored in order; only the last `2*window`
        are kept.
        """
        for tu in tool_uses:
            self._last_tool_hashes.append(_hash_tool_use(tu))
        # cap history
        keep = max(self.repetition_window * 2, 4)
        if len(self._last_tool_hashes) > keep:
            self._last_tool_hashes = self._last_tool_hashes[-keep:]

    def check(
        self,
        *,
        next_call_token_estimate: int = 0,
        new_tool_uses: tuple[ToolUseBlock, ...] | None = None,
    ) -> StopReason | None:
        """Return the reason to stop, or None to continue.

        Called by the loop before each model call.
        """
        if self._step >= self.max_steps:
            return StopReason.MAX_STEPS
        if time.monotonic() - self._started_at >= self.wall_clock_seconds:
            return StopReason.WALL_CLOCK
        if self._tokens_used + next_call_token_estimate > self.token_budget:
            return StopReason.TOKEN_BUDGET
        # If the model just asked to call the same tool twice in a row with
        # the same args, that's "no progress".
        if new_tool_uses:
            new_hashes = [_hash_tool_use(t) for t in new_tool_uses]
            tail = self._last_tool_hashes[-len(new_hashes) :] if self._last_tool_hashes else []
            if tail and tail == new_hashes:
                return StopReason.NO_PROGRESS
            # And if the last `window` calls (including the new one) are all
            # identical, that's a degenerate repetition.
            recent = (self._last_tool_hashes + new_hashes)[-self.repetition_window :]
            if len(recent) >= self.repetition_window and all(h == recent[0] for h in recent):
                return StopReason.REPETITION
        return None


def _hash_tool_use(tu: ToolUseBlock) -> str:
    """Stable hash of (name, normalised args) — used for repetition detection."""
    blob = json.dumps({"name": tu.name, "args": tu.input}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

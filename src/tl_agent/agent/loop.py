"""The ReACT loop — Phase 5's deep-dive driver.

Senior signals encoded here:
- Explicit `step()` / `stop()` control flow — `run()` is a thin driver that
  calls `step()` (one iteration) until it returns a terminal result; `stop()`
  is the single named predicate every halt decision flows through.
- Stop reasons are an enum, recorded on the result, queryable from traces.
- Tools come from the read-only registry binding (writers cannot leak in).
- Idempotency + retry live in `BaseTool.invoke`; this loop never bypasses it.
- Optional verifier; on RETRY_WITH_HINT, the hint is added as a user message
  and we re-loop bounded by `verifier_max_retries`.
- The success-claim-without-action detector runs cheap on every termination
  before we escalate to the LLM verifier.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from tl_agent.agent.stop_conditions import StopConditions, StopReason
from tl_agent.agent.verifier import (
    Verifier,
    VerifierVerdict,
    check_success_claim,
    format_transcript_for_verifier,
)
from tl_agent.llm.base import (
    CompletionRequest,
    Message,
    MessageRole,
    Provider,
    ProviderError,
    ToolResultBlock,
    ToolUseBlock,
)
from tl_agent.obs.spans import phase_span
from tl_agent.tools.base import BaseTool, IdempotencyLookup, ToolError, ToolResult

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)


# -------------------- public result --------------------


@dataclass(frozen=True)
class AgentLoopResult:
    """What the ReACT loop returns to the caller (Phase 5)."""

    final_text: str
    stop_reason: StopReason
    verifier_verdict: VerifierVerdict | None
    verifier_reason: str | None
    steps_taken: int
    tool_calls_made: int
    claim_mismatches: int
    transcript: tuple[Message, ...]


# -------------------- per-run mutable state --------------------


@dataclass
class _StepState:
    """The mutable state threaded through `step()` across one `run()`.

    Pulling it out of the loop body is what lets `step()` be a real, callable
    single-iteration method instead of an inline `while True:` body — the
    driver owns the loop, `step()` owns one iteration, `stop()` owns the
    decision to halt.
    """

    task: str
    messages: list[Message]
    tool_by_name: dict[str, BaseTool[Any, Any]]
    anthropic_tools: tuple[dict[str, Any], ...]
    tools_called: set[str] = field(default_factory=set[str])
    total_tool_calls: int = 0
    retries_used: int = 0
    last_text: str = ""


# -------------------- the loop --------------------


@dataclass
class AgentLoop:
    """One ReACT loop instance — build per hotspot.

    Caller wiring:
        loop = AgentLoop(
            provider=provider,
            model=route.model,
            tools=[t1, t2, ...],
            system=system_prompt,
            stop_conditions=StopConditions(...),
            verifier=verifier_instance,        # optional
            idempotency=store,                 # for any writer tools
        )
        result = await loop.run(task="Why is ENG-12 stuck?")
    """

    provider: Provider
    model: str
    tools: list[BaseTool[Any, Any]]
    system: str
    stop_conditions: StopConditions = field(default_factory=StopConditions)
    verifier: Verifier | None = None
    idempotency: IdempotencyLookup | None = None
    run_date_iso: str = ""
    verifier_max_retries: int = 1
    expected_tools_for_phrases: Mapping[str, set[str]] | None = None

    @phase_span("phase5_deepdive_loop")
    async def run(self, *, task: str) -> AgentLoopResult:
        """Drive the ReACT loop to termination.

        Deliberately a thin driver: build the initial state, then call
        `step()` until it hands back a terminal `AgentLoopResult`. No control
        logic lives here — each iteration's work is in `step()` and every halt
        decision is in `stop()`, so the loop is inspectable and the stop reason
        is always an explicit `StopReason`.
        """
        state = _StepState(
            task=task,
            messages=[Message(role=MessageRole.USER, content=task)],
            tool_by_name={t.name: t for t in self.tools},
            anthropic_tools=tuple(_tool_schema(t) for t in self.tools),
        )
        result: AgentLoopResult | None = None
        while result is None:
            result = await self.step(state)
        return result

    def stop(
        self,
        state: _StepState,
        *,
        next_call_token_estimate: int = 0,
        new_tool_uses: tuple[ToolUseBlock, ...] | None = None,
    ) -> StopReason | None:
        """The loop's termination predicate. Returns the `StopReason` to halt
        on, or `None` to keep stepping.

        Every reason the loop may stop flows through this one named call, so a
        trace shows exactly which condition fired. The stateful budget / clock /
        repetition bookkeeping is delegated to `StopConditions`.
        """
        return self.stop_conditions.check(
            next_call_token_estimate=next_call_token_estimate,
            new_tool_uses=new_tool_uses,
        )

    async def step(self, state: _StepState) -> AgentLoopResult | None:
        """Run exactly one ReACT iteration against `state`.

        Returns a terminal `AgentLoopResult` when the loop should stop, or
        `None` to signal "keep going" (the model produced tool calls we
        executed, or the verifier asked for one more round).
        """
        self.stop_conditions.step_start()

        # Pre-call budget / clock check.
        estimate = self.provider.estimate_tokens(self.system + state.task) // 2
        stop = self.stop(state, next_call_token_estimate=estimate)
        if stop is not None:
            return _finalise(
                state.last_text or "(no answer — stopped early)",
                stop_reason=stop,
                verifier_verdict=None,
                verifier_reason=None,
                steps=self.stop_conditions.step,
                tool_calls=state.total_tool_calls,
                transcript=tuple(state.messages),
                claim_mismatches=0,
            )

        try:
            resp = await self.provider.complete(
                CompletionRequest(
                    model=self.model,
                    messages=tuple(state.messages),
                    system=self.system,
                    max_tokens=2048,
                    temperature=0.0,
                    tools=state.anthropic_tools,
                    tool_choice="auto",
                    cache_system=True,
                    phase="phase5_deepdive",
                )
            )
        except ProviderError as exc:
            logger.exception("react.provider_error", extra={"err": str(exc)})
            return _finalise(
                state.last_text or f"(provider error: {exc})",
                stop_reason=StopReason.ERROR,
                verifier_verdict=None,
                verifier_reason=str(exc),
                steps=self.stop_conditions.step,
                tool_calls=state.total_tool_calls,
                transcript=tuple(state.messages),
                claim_mismatches=0,
            )

        self.stop_conditions.add_tokens(resp.usage.input_tokens + resp.usage.output_tokens)
        state.last_text = resp.text or state.last_text

        # Append assistant turn (text + any tool_use blocks).
        state.messages.append(
            Message(
                role=MessageRole.ASSISTANT,
                content=resp.text,
                tool_uses=resp.tool_uses,
            )
        )

        # No tool calls — normal end of turn; verdict via verifier path.
        if not resp.tool_uses:
            return await self._finish_turn(state, resp.text)

        # Repetition / no-progress check on the NEW tool_uses.
        stop = self.stop(state, new_tool_uses=resp.tool_uses)
        if stop is not None:
            return _finalise(
                state.last_text or "(stopped due to repetition)",
                stop_reason=stop,
                verifier_verdict=None,
                verifier_reason=None,
                steps=self.stop_conditions.step,
                tool_calls=state.total_tool_calls,
                transcript=tuple(state.messages),
                claim_mismatches=0,
            )
        self.stop_conditions.record_tool_use(resp.tool_uses)

        await self._execute_tool_uses(state, resp.tool_uses)
        return None

    async def _finish_turn(self, state: _StepState, final_text: str) -> AgentLoopResult | None:
        """Handle an end-of-turn (no tool calls): claim check + optional verifier.

        Returns the terminal result, or `None` if the verifier injected a hint
        and the loop should take one more step.
        """
        stop_reason = StopReason.END_TURN
        # Cheap deterministic check first.
        mismatches = check_success_claim(
            final_text=final_text,
            tool_names_called=state.tools_called,
            expected_tools_for_phrases=dict(self.expected_tools_for_phrases or {}),
        )
        # LLM verifier (optional).
        verdict: VerifierVerdict | None = None
        v_reason: str | None = None
        if self.verifier is not None:
            transcript_text = format_transcript_for_verifier(state.messages)
            v_result = await self.verifier.verify(
                task=state.task,
                tool_transcript=transcript_text,
                final_artifact=final_text,
            )
            verdict = v_result.verdict
            v_reason = v_result.reason
            if (
                v_result.verdict is VerifierVerdict.RETRY_WITH_HINT
                and state.retries_used < self.verifier_max_retries
                and v_result.hint_for_retry
            ):
                state.retries_used += 1
                state.messages.append(
                    Message(
                        role=MessageRole.USER,
                        content=(
                            "Verifier suggests one more step before you can finish: "
                            f"{v_result.hint_for_retry}"
                        ),
                    )
                )
                return None
            if v_result.verdict is VerifierVerdict.PASS:
                stop_reason = StopReason.VERIFIER_PASSED
            else:
                stop_reason = StopReason.VERIFIER_FAILED
        return _finalise(
            final_text,
            stop_reason=stop_reason,
            verifier_verdict=verdict,
            verifier_reason=v_reason,
            steps=self.stop_conditions.step,
            tool_calls=state.total_tool_calls,
            transcript=tuple(state.messages),
            claim_mismatches=len(mismatches),
        )

    async def _execute_tool_uses(
        self, state: _StepState, tool_uses: tuple[ToolUseBlock, ...]
    ) -> None:
        """Execute the model's tool calls and append the results to the transcript.

        Sequential within one assistant turn — the model expects results in the
        order it asked.
        """
        tool_results: list[ToolResultBlock] = []
        for tu in tool_uses:
            state.total_tool_calls += 1
            state.tools_called.add(tu.name)
            tool = state.tool_by_name.get(tu.name)
            if tool is None:
                tool_results.append(
                    ToolResultBlock(
                        tool_use_id=tu.id,
                        content=f"unknown tool '{tu.name}'",
                        is_error=True,
                    )
                )
                continue
            result = await tool.invoke(
                tu.input,
                run_date_iso=self.run_date_iso,
                idempotency_lookup=self.idempotency,
            )
            tool_results.append(_to_tool_result_block(tu.id, result))

        state.messages.append(Message(role=MessageRole.TOOL, tool_results=tuple(tool_results)))


# -------------------- helpers --------------------


def _tool_schema(tool: BaseTool[Any, Any]) -> dict[str, Any]:
    """Anthropic-style tool def for one tool, generated from its Pydantic input."""
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_model.model_json_schema(),
    }


def _to_tool_result_block(tool_use_id: str, result: object) -> ToolResultBlock:
    """Convert BaseTool.invoke output into the Anthropic-style result block."""
    if isinstance(result, ToolResult):
        try:
            content = cast(ToolResult[Any], result).value.model_dump_json()  # type: ignore[redundant-cast]
        except Exception as exc:
            return ToolResultBlock(
                tool_use_id=tool_use_id,
                content=f"(serialisation error: {exc})",
                is_error=True,
            )
        return ToolResultBlock(tool_use_id=tool_use_id, content=content, is_error=False)
    if isinstance(result, ToolError):
        return ToolResultBlock(
            tool_use_id=tool_use_id,
            content=f"[{result.kind.value}] {result.message}",
            is_error=True,
        )
    return ToolResultBlock(
        tool_use_id=tool_use_id,
        content=f"(unexpected tool result type: {type(result).__name__})",
        is_error=True,
    )


def _finalise(
    text: str,
    *,
    stop_reason: StopReason,
    verifier_verdict: VerifierVerdict | None,
    verifier_reason: str | None,
    steps: int,
    tool_calls: int,
    transcript: tuple[Message, ...],
    claim_mismatches: int,
) -> AgentLoopResult:
    logger.info(
        "react.finalise",
        extra={
            "stop_reason": stop_reason.value,
            "verifier_verdict": verifier_verdict.value if verifier_verdict else None,
            "steps": steps,
            "tool_calls": tool_calls,
            "claim_mismatches": claim_mismatches,
        },
    )
    return AgentLoopResult(
        final_text=text,
        stop_reason=stop_reason,
        verifier_verdict=verifier_verdict,
        verifier_reason=verifier_reason,
        steps_taken=steps,
        tool_calls_made=tool_calls,
        claim_mismatches=claim_mismatches,
        transcript=transcript,
    )

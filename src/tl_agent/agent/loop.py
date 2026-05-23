"""The ReACT loop — Phase 5's deep-dive driver.

Senior signals encoded here:
- Explicit `step()` / `stop()` control flow — NOT `while True`.
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
from typing import TYPE_CHECKING, Any

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
        messages: list[Message] = [Message(role=MessageRole.USER, content=task)]
        tool_by_name = {t.name: t for t in self.tools}
        anthropic_tools = tuple(_tool_schema(t) for t in self.tools)
        tools_called: set[str] = set()
        total_tool_calls = 0
        retries_used = 0
        last_text = ""

        while True:
            self.stop_conditions.step_start()

            # Pre-call budget / clock check
            stop = self.stop_conditions.check(
                next_call_token_estimate=self.provider.estimate_tokens(self.system + task) // 2
            )
            if stop is not None:
                return _finalise(
                    last_text or "(no answer — stopped early)",
                    stop_reason=stop,
                    verifier_verdict=None,
                    verifier_reason=None,
                    steps=self.stop_conditions.step,
                    tool_calls=total_tool_calls,
                    transcript=tuple(messages),
                    claim_mismatches=0,
                )

            try:
                resp = await self.provider.complete(
                    CompletionRequest(
                        model=self.model,
                        messages=tuple(messages),
                        system=self.system,
                        max_tokens=2048,
                        temperature=0.0,
                        tools=anthropic_tools,
                        tool_choice="auto",
                        cache_system=True,
                        phase="phase5_deepdive",
                    )
                )
            except ProviderError as exc:
                logger.exception("react.provider_error", extra={"err": str(exc)})
                return _finalise(
                    last_text or f"(provider error: {exc})",
                    stop_reason=StopReason.ERROR,
                    verifier_verdict=None,
                    verifier_reason=str(exc),
                    steps=self.stop_conditions.step,
                    tool_calls=total_tool_calls,
                    transcript=tuple(messages),
                    claim_mismatches=0,
                )

            self.stop_conditions.add_tokens(resp.usage.input_tokens + resp.usage.output_tokens)
            last_text = resp.text or last_text

            # Append assistant turn (text + any tool_use blocks)
            messages.append(
                Message(
                    role=MessageRole.ASSISTANT,
                    content=resp.text,
                    tool_uses=resp.tool_uses,
                )
            )

            # No tool calls — normal end of turn.
            if not resp.tool_uses:
                stop_reason = StopReason.END_TURN
                # Cheap deterministic check first
                mismatches = check_success_claim(
                    final_text=resp.text,
                    tool_names_called=tools_called,
                    expected_tools_for_phrases=dict(self.expected_tools_for_phrases or {}),
                )
                # LLM verifier (optional)
                verdict: VerifierVerdict | None = None
                v_reason: str | None = None
                if self.verifier is not None:
                    transcript_text = format_transcript_for_verifier(messages)
                    v_result = await self.verifier.verify(
                        task=task,
                        tool_transcript=transcript_text,
                        final_artifact=resp.text,
                    )
                    verdict = v_result.verdict
                    v_reason = v_result.reason
                    if (
                        v_result.verdict is VerifierVerdict.RETRY_WITH_HINT
                        and retries_used < self.verifier_max_retries
                        and v_result.hint_for_retry
                    ):
                        retries_used += 1
                        messages.append(
                            Message(
                                role=MessageRole.USER,
                                content=(
                                    "Verifier suggests one more step before you can finish: "
                                    f"{v_result.hint_for_retry}"
                                ),
                            )
                        )
                        continue
                    if v_result.verdict is VerifierVerdict.PASS:
                        stop_reason = StopReason.VERIFIER_PASSED
                    else:
                        stop_reason = StopReason.VERIFIER_FAILED
                return _finalise(
                    resp.text,
                    stop_reason=stop_reason,
                    verifier_verdict=verdict,
                    verifier_reason=v_reason,
                    steps=self.stop_conditions.step,
                    tool_calls=total_tool_calls,
                    transcript=tuple(messages),
                    claim_mismatches=len(mismatches),
                )

            # Repetition / no-progress check on the NEW tool_uses
            stop = self.stop_conditions.check(new_tool_uses=resp.tool_uses)
            if stop is not None:
                return _finalise(
                    last_text or "(stopped due to repetition)",
                    stop_reason=stop,
                    verifier_verdict=None,
                    verifier_reason=None,
                    steps=self.stop_conditions.step,
                    tool_calls=total_tool_calls,
                    transcript=tuple(messages),
                    claim_mismatches=0,
                )
            self.stop_conditions.record_tool_use(resp.tool_uses)

            # Execute tools (sequential within one assistant turn — the model
            # expects results in the order it asked).
            tool_results: list[ToolResultBlock] = []
            for tu in resp.tool_uses:
                total_tool_calls += 1
                tools_called.add(tu.name)
                tool = tool_by_name.get(tu.name)
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

            # Hand results back; loop
            messages.append(Message(role=MessageRole.TOOL, tool_results=tuple(tool_results)))


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
            content = result.value.model_dump_json()
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

"""Tests for stop conditions, verifier, and the ReACT loop.

We stub the Provider so no network is touched. The loop is driven by a
scripted sequence of CompletionResponses — each test wires up the script
that demonstrates the exact behaviour under test.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from tl_agent.agent import (
    AgentLoop,
    StopConditions,
    StopReason,
    Verifier,
    VerifierResult,
    VerifierVerdict,
    check_success_claim,
)
from tl_agent.llm.base import (
    CompletionRequest,
    CompletionResponse,
    Provider,
    TokenUsage,
    ToolUseBlock,
)
from tl_agent.tools.base import BaseTool

# -------------------- toy tools --------------------


class _GetTicket(BaseModel):
    key: str


class _Ticket(BaseModel):
    key: str
    status: str


class GetTicketTool(BaseTool[_GetTicket, _Ticket]):
    name: ClassVar[str] = "get_ticket"
    description: ClassVar[str] = "Fetch a ticket."
    input_model: ClassVar[type[BaseModel]] = _GetTicket
    output_model: ClassVar[type[BaseModel]] = _Ticket
    call_log: ClassVar[list[str]] = []

    async def _call(self, args: _GetTicket) -> _Ticket:
        type(self).call_log.append(args.key)
        return _Ticket(key=args.key, status="in_progress")


class _GetDeps(BaseModel):
    key: str


class _Deps(BaseModel):
    blocks: list[str]
    blocked_by: list[str]


class GetDependenciesTool(BaseTool[_GetDeps, _Deps]):
    name: ClassVar[str] = "get_ticket_dependencies"
    description: ClassVar[str] = "Fetch the dependency graph for a ticket."
    input_model: ClassVar[type[BaseModel]] = _GetDeps
    output_model: ClassVar[type[BaseModel]] = _Deps

    async def _call(self, args: _GetDeps) -> _Deps:
        del args
        return _Deps(blocks=["ENG-19"], blocked_by=[])


# -------------------- scripted provider --------------------


class ScriptedProvider(Provider):
    """Pops the next pre-canned `CompletionResponse` per `complete()` call."""

    name = "anthropic"

    def __init__(self, script: list[CompletionResponse]) -> None:
        self._script = list(script)
        self.calls: list[CompletionRequest] = []

    async def complete(self, req: CompletionRequest) -> CompletionResponse:
        self.calls.append(req)
        if not self._script:
            raise AssertionError("scripted provider exhausted")
        return self._script.pop(0)

    async def structured[T: BaseModel](
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: type[T],
        max_tokens: int = 1024,
        temperature: float = 0.0,
        cache_system: bool = False,
        phase: str | None = None,
    ) -> tuple[T, TokenUsage]:
        del model, system, user, max_tokens, temperature, cache_system, phase
        # Used by Verifier under tests.
        if hasattr(self, "_structured_return"):
            return self._structured_return, TokenUsage()  # type: ignore[attr-defined]
        return schema.model_validate({}), TokenUsage()

    def estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def set_structured_return(self, value: Any) -> None:
        self._structured_return = value  # type: ignore[attr-defined]


def _resp(text: str = "", *tool_uses: ToolUseBlock) -> CompletionResponse:
    return CompletionResponse(
        text=text,
        tool_uses=tuple(tool_uses),
        stop_reason="tool_use" if tool_uses else "end_turn",
        usage=TokenUsage(input_tokens=10, output_tokens=5),
    )


# -------------------- StopConditions --------------------


def test_stop_max_steps() -> None:
    sc = StopConditions(max_steps=2, wall_clock_seconds=1000, token_budget=1_000_000)
    sc.step_start()
    sc.step_start()
    assert sc.check() == StopReason.MAX_STEPS


def test_stop_no_progress_same_tool_twice() -> None:
    sc = StopConditions(max_steps=99, repetition_window=99)
    first = ToolUseBlock(id="t1", name="get_ticket", input={"key": "ENG-12"})
    sc.record_tool_use((first,))
    same = ToolUseBlock(id="t2", name="get_ticket", input={"key": "ENG-12"})
    assert sc.check(new_tool_uses=(same,)) == StopReason.NO_PROGRESS


def test_stop_repetition_window() -> None:
    sc = StopConditions(max_steps=99, repetition_window=3)
    same1 = ToolUseBlock(id="t1", name="get_ticket", input={"key": "ENG-12"})
    same2 = ToolUseBlock(id="t2", name="get_ticket", input={"key": "ENG-12"})
    sc.record_tool_use((same1, same2))
    same3 = ToolUseBlock(id="t3", name="get_ticket", input={"key": "ENG-12"})
    # Third identical call closes the repetition window.
    assert sc.check(new_tool_uses=(same3,)) in {StopReason.REPETITION, StopReason.NO_PROGRESS}


# -------------------- check_success_claim --------------------


def test_check_success_claim_catches_unsupported_claim() -> None:
    final = "I checked the ticket dependencies and saw no blockers. Looks fine."
    mismatches = check_success_claim(
        final,
        tool_names_called={"get_ticket"},  # missing get_ticket_dependencies
    )
    assert mismatches
    assert any("ticket dependencies" in m.explanation for m in mismatches)


def test_check_success_claim_accepts_supported_claim() -> None:
    final = "I checked the ticket dependencies and ENG-12 is blocked by ENG-9."
    mismatches = check_success_claim(final, tool_names_called={"get_ticket_dependencies"})
    assert not mismatches


def test_check_success_claim_flags_verdict_with_no_tools() -> None:
    final = "All clear, no issue."
    mismatches = check_success_claim(final, tool_names_called=set())
    assert mismatches


# -------------------- AgentLoop happy path --------------------


async def test_loop_executes_tool_then_finishes() -> None:
    GetTicketTool.call_log.clear()
    tool_call = ToolUseBlock(id="tu1", name="get_ticket", input={"key": "ENG-12"})
    script = [
        _resp("Let me check.", tool_call),
        _resp("ENG-12 is in_progress."),
    ]
    provider = ScriptedProvider(script)
    loop = AgentLoop(
        provider=provider,
        model="m",
        tools=[GetTicketTool()],
        system="be helpful",
        run_date_iso="2026-05-22",
    )
    result = await loop.run(task="What is ENG-12's status?")
    assert result.stop_reason == StopReason.END_TURN
    assert result.tool_calls_made == 1
    assert "ENG-12" in result.final_text
    assert GetTicketTool.call_log == ["ENG-12"]


async def test_loop_repetition_halts() -> None:
    """Same tool+args three times in a row → REPETITION."""
    same = ToolUseBlock(id="tu1", name="get_ticket", input={"key": "ENG-12"})
    script = [_resp("call 1", same), _resp("call 2", same), _resp("call 3", same)]
    provider = ScriptedProvider(script)
    loop = AgentLoop(
        provider=provider,
        model="m",
        tools=[GetTicketTool()],
        system="be helpful",
        stop_conditions=StopConditions(repetition_window=3, max_steps=10),
        run_date_iso="2026-05-22",
    )
    result = await loop.run(task="loop test")
    assert result.stop_reason in {StopReason.REPETITION, StopReason.NO_PROGRESS}


async def test_loop_max_steps_caps_runaway() -> None:
    # Always asks for a tool; vary args so repetition/no-progress don't fire first.
    script = [
        _resp(
            f"looping {i}",
            ToolUseBlock(id=f"tu{i}", name="get_ticket", input={"key": f"ENG-{12 + i}"}),
        )
        for i in range(20)
    ]
    provider = ScriptedProvider(script)
    loop = AgentLoop(
        provider=provider,
        model="m",
        tools=[GetTicketTool()],
        system="be helpful",
        stop_conditions=StopConditions(max_steps=3, repetition_window=99),
        run_date_iso="2026-05-22",
    )
    result = await loop.run(task="runaway")
    assert result.stop_reason == StopReason.MAX_STEPS


async def test_loop_unknown_tool_returns_error_block() -> None:
    bad = ToolUseBlock(id="tu1", name="not_a_real_tool", input={})
    script = [_resp("trying", bad), _resp("done")]
    provider = ScriptedProvider(script)
    loop = AgentLoop(
        provider=provider,
        model="m",
        tools=[GetTicketTool()],
        system="be helpful",
        run_date_iso="2026-05-22",
    )
    result = await loop.run(task="bad tool")
    assert result.stop_reason == StopReason.END_TURN
    # The tool_result block should have surfaced an error string.
    tool_msg = result.transcript[2]  # user, assistant, tool, assistant
    assert tool_msg.tool_results[0].is_error


# -------------------- Verifier integration --------------------


class _ScriptedVerifier:
    """Stand-in for Verifier that returns a pre-canned result."""

    def __init__(self, result: VerifierResult) -> None:
        self.result = result
        self.calls = 0

    async def verify(
        self, *, task: str, tool_transcript: str, final_artifact: str
    ) -> VerifierResult:
        del task, tool_transcript, final_artifact
        self.calls += 1
        return self.result


async def test_loop_verifier_pass_terminates_with_verifier_passed() -> None:
    script = [_resp("Done.")]
    provider = ScriptedProvider(script)
    verifier = _ScriptedVerifier(VerifierResult(VerifierVerdict.PASS, "looks good"))
    loop = AgentLoop(
        provider=provider,
        model="m",
        tools=[GetTicketTool()],
        system="be helpful",
        verifier=verifier,  # type: ignore[arg-type] — duck-typed
        run_date_iso="2026-05-22",
    )
    result = await loop.run(task="finish quick")
    assert result.stop_reason == StopReason.VERIFIER_PASSED
    assert verifier.calls == 1


async def test_loop_verifier_retry_with_hint_loops_once() -> None:
    tu_after_hint = ToolUseBlock(id="tu1", name="get_ticket_dependencies", input={"key": "ENG-12"})
    script = [
        _resp("Done."),  # initial finish
        _resp("checking deps", tu_after_hint),  # after the hint
        _resp("Now I'm done."),  # final after dep tool result
    ]
    provider = ScriptedProvider(script)
    hint = "Check the dependency graph before concluding."
    # First call: RETRY_WITH_HINT; subsequent: PASS
    call_count = {"n": 0}

    def verify_side_effect() -> VerifierResult:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return VerifierResult(
                VerifierVerdict.RETRY_WITH_HINT, "missed deps", hint_for_retry=hint
            )
        return VerifierResult(VerifierVerdict.PASS, "looks good now")

    class _DynamicVerifier:
        def __init__(self, fn: Callable[[], VerifierResult]) -> None:
            self.fn = fn

        async def verify(
            self, *, task: str, tool_transcript: str, final_artifact: str
        ) -> VerifierResult:
            del task, tool_transcript, final_artifact
            return self.fn()

    loop = AgentLoop(
        provider=provider,
        model="m",
        tools=[GetTicketTool(), GetDependenciesTool()],
        system="be helpful",
        verifier=_DynamicVerifier(verify_side_effect),  # type: ignore[arg-type]
        verifier_max_retries=1,
        run_date_iso="2026-05-22",
    )
    result = await loop.run(task="deps test")
    assert result.stop_reason == StopReason.VERIFIER_PASSED
    assert result.tool_calls_made == 1


def test_verifier_uses_structured_call() -> None:
    """Smoke that the Verifier class wires through to provider.structured."""
    provider = ScriptedProvider([])
    from tl_agent.agent.verifier import _VerifierOutput

    provider.set_structured_return(
        _VerifierOutput(verdict=VerifierVerdict.PASS, reason="ok", hint_for_retry=None)
    )
    v = Verifier(provider, model="verifier-m")
    import asyncio

    result = asyncio.run(v.verify(task="t", tool_transcript="", final_artifact="x"))
    assert result.verdict == VerifierVerdict.PASS


@pytest.mark.parametrize(
    "verdict,want_stop",
    [
        (VerifierVerdict.FAIL, StopReason.VERIFIER_FAILED),
        (VerifierVerdict.PASS, StopReason.VERIFIER_PASSED),
    ],
)
async def test_loop_verifier_terminal_verdicts(
    verdict: VerifierVerdict, want_stop: StopReason
) -> None:
    script = [_resp("Done.")]
    provider = ScriptedProvider(script)
    verifier = _ScriptedVerifier(VerifierResult(verdict, "x"))
    loop = AgentLoop(
        provider=provider,
        model="m",
        tools=[GetTicketTool()],
        system="be helpful",
        verifier=verifier,  # type: ignore[arg-type]
        run_date_iso="2026-05-22",
    )
    result = await loop.run(task="terminal")
    assert result.stop_reason == want_stop

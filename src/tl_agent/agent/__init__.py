"""Agent layer — ReACT loop, stop conditions, verifier, concurrency."""

from tl_agent.agent.concurrency import ConcurrencyGate, fan_out
from tl_agent.agent.loop import AgentLoop, AgentLoopResult
from tl_agent.agent.stop_conditions import StopConditions, StopReason
from tl_agent.agent.verifier import (
    ClaimMismatch,
    Verifier,
    VerifierResult,
    VerifierVerdict,
    check_success_claim,
    format_transcript_for_verifier,
)

__all__ = [
    "AgentLoop",
    "AgentLoopResult",
    "ClaimMismatch",
    "ConcurrencyGate",
    "StopConditions",
    "StopReason",
    "Verifier",
    "VerifierResult",
    "VerifierVerdict",
    "check_success_claim",
    "fan_out",
    "format_transcript_for_verifier",
]

"""Verifier — the "did the agent actually do what it claimed" guard.

Two pieces:

1. `check_success_claim(...)` — deterministic. Catches the most common
   failure: the agent's final message claims to have done X but the tool
   transcript shows no call that could have produced X. This is "the agent
   confidently lied to me." No LLM needed.

2. `Verifier.verify(...)` — LLM-driven. A separate Sonnet call reads
   the produced artifact + the tool transcript and returns
   PASS / RETRY_WITH_HINT(reason) / FAIL.

The loop calls (1) cheaply on every termination; only escalates to (2) when
the artifact looks suspicious or the prompt explicitly asks for it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from tl_agent.llm.base import Message, MessageRole, Provider, ProviderError


class VerifierVerdict(StrEnum):
    """The three outcomes the verifier can render."""

    PASS = "pass"  # noqa: S105 — enum string, not a credential
    RETRY_WITH_HINT = "retry_with_hint"
    FAIL = "fail"


class _VerifierOutput(BaseModel):
    """Schema returned by the verifier LLM call."""

    model_config = ConfigDict(extra="forbid")

    verdict: VerifierVerdict
    reason: str = Field(min_length=1, max_length=400)
    hint_for_retry: str | None = Field(default=None, max_length=400)


@dataclass(frozen=True)
class VerifierResult:
    verdict: VerifierVerdict
    reason: str
    hint_for_retry: str | None = None


# ---------- deterministic success-claim check ----------


_CLAIM_VERBS = (
    "i checked",
    "i looked at",
    "i fetched",
    "i found",
    "i verified",
    "i confirmed",
    "i identified",
    "i pulled",
    "i reviewed",
    "i inspected",
)


@dataclass(frozen=True)
class ClaimMismatch:
    """One unsupported claim and the tool(s) that would have backed it."""

    claim: str
    expected_tool_hint: str
    explanation: str


def check_success_claim(
    final_text: str,
    *,
    tool_names_called: set[str],
    expected_tools_for_phrases: dict[str, set[str]] | None = None,
) -> list[ClaimMismatch]:
    """Detect lines in `final_text` that *claim* to have used a tool, but the
    transcript shows no call to it.

    `expected_tools_for_phrases` lets callers register domain-specific
    mappings, e.g. {"ticket dependencies": {"get_ticket_dependencies"}}.
    A default heuristic covers common verbs.

    Returns a list of mismatches. Empty list ⇒ the claim is supported.
    """
    expected_tools_for_phrases = expected_tools_for_phrases or _DEFAULT_PHRASE_TOOLS
    mismatches: list[ClaimMismatch] = []
    lower = final_text.lower()

    sentences = re.split(r"(?<=[.!?])\s+", final_text)
    for sentence in sentences:
        s_lower = sentence.lower()
        if not any(verb in s_lower for verb in _CLAIM_VERBS):
            continue
        for phrase, candidates in expected_tools_for_phrases.items():
            if phrase in s_lower and not (candidates & tool_names_called):
                mismatches.append(
                    ClaimMismatch(
                        claim=sentence.strip(),
                        expected_tool_hint=" / ".join(sorted(candidates)),
                        explanation=(
                            f'claim mentions "{phrase}" but transcript has no call '
                            f"to any of: {sorted(candidates)}"
                        ),
                    )
                )
    # If the model used a very confident closing without any tool calls AT
    # ALL, that itself is suspicious for a tool-required prompt.
    if not tool_names_called and any(
        marker in lower for marker in ("looks fine", "all clear", "no issue", "i recommend")
    ):
        mismatches.append(
            ClaimMismatch(
                claim=final_text.strip()[:160],
                expected_tool_hint="(any)",
                explanation="confident verdict reached without invoking any tool",
            )
        )
    return mismatches


_DEFAULT_PHRASE_TOOLS: dict[str, set[str]] = {
    "ticket dependencies": {"get_ticket_dependencies"},
    "ticket history": {"get_ticket_history"},
    "commit": {"get_commit_diff", "list_commits"},
    "commit diff": {"get_commit_diff"},
    "standup history": {"search_standup_history"},
    "baseline": {"get_baseline"},
}


# ---------- LLM-driven verifier ----------


VERIFIER_SYSTEM = """\
You are a verifier. You audit a junior agent's work.

You are given:
  1. The original task description.
  2. The list of tool calls the agent actually made (name + args + result snippet).
  3. The agent's final answer / artifact.

You must decide:
  - PASS — the answer is fully supported by the tool transcript and the task.
  - RETRY_WITH_HINT — the answer is plausible but the agent missed a specific
    investigation that would resolve a remaining ambiguity. Provide a
    one-sentence hint naming the missing step.
  - FAIL — the answer contradicts the tool transcript, makes up evidence,
    or claims to have done something that did not happen.

Respond ONLY with structured JSON conforming to the provided schema. Be
strict — when in doubt between PASS and RETRY_WITH_HINT, choose
RETRY_WITH_HINT. The cost of a bad PASS is much higher than the cost of one
extra investigation round.
"""


class Verifier:
    """LLM-driven verifier.

    The verifier MUST use a different (cheaper, faster) model than the agent
    it audits — both for cost and to surface bias when one model fools itself.
    Default route is `verifier` in config/router.yaml (Sonnet 4.6 today).
    """

    def __init__(self, provider: Provider, *, model: str, max_tokens: int = 512) -> None:
        self._provider = provider
        self._model = model
        self._max_tokens = max_tokens

    async def verify(
        self,
        *,
        task: str,
        tool_transcript: str,
        final_artifact: str,
    ) -> VerifierResult:
        """Run the verifier on one (task, transcript, artifact) triple."""
        user = (
            f"# Task\n{task}\n\n"
            f"# Tool transcript\n{tool_transcript}\n\n"
            f"# Agent final artifact\n{final_artifact}\n"
        )
        try:
            output, _usage = await self._provider.structured(
                model=self._model,
                system=VERIFIER_SYSTEM,
                user=user,
                schema=_VerifierOutput,
                max_tokens=self._max_tokens,
                temperature=0.0,
                phase="verifier",
            )
        except ProviderError as exc:
            # Verifier failure is non-fatal — return a soft FAIL so the loop
            # knows the answer wasn't blessed, but doesn't crash the run.
            return VerifierResult(
                verdict=VerifierVerdict.FAIL,
                reason=f"verifier provider error: {exc}",
            )
        return VerifierResult(
            verdict=output.verdict,
            reason=output.reason,
            hint_for_retry=output.hint_for_retry,
        )


def format_transcript_for_verifier(
    messages: list[Message],
) -> str:
    """Convert a ReACT message log to a compact textual transcript.

    Keeps the verifier's input bounded; the goal is "enough to audit", not
    a full replay.
    """
    lines: list[str] = []
    for m in messages:
        if m.role is MessageRole.ASSISTANT:
            if m.content:
                lines.append(f"AGENT: {m.content.strip()[:400]}")
            for tu in m.tool_uses:
                args_short = str(tu.input)[:240]
                lines.append(f"TOOL_CALL {tu.name}({args_short})")
        elif m.role is MessageRole.TOOL:
            for tr in m.tool_results:
                snippet = tr.content[:280].replace("\n", " ")
                tag = "ERROR" if tr.is_error else "OK"
                lines.append(f"TOOL_RESULT[{tag}] {snippet}")
        elif m.role is MessageRole.USER:
            if m.content:
                lines.append(f"USER: {m.content.strip()[:240]}")
    return "\n".join(lines)

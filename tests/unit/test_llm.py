"""LLM router + budget + provider plumbing.

We don't hit any network — providers are stubbed via the same Provider ABC.
The Anthropic/Ollama providers themselves get integration tests later that
exercise the SDK against a recorded fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from tl_agent.llm import (
    BudgetExceeded,
    BudgetTracker,
    CompletionRequest,
    CompletionResponse,
    Message,
    MessageRole,
    Provider,
    Router,
    RouterConfig,
    TokenUsage,
    ToolUseBlock,
)

# -------------------- BudgetTracker --------------------


def test_budget_tracker_allows_under_cap() -> None:
    b = BudgetTracker(token_cap=1_000)
    b.check(projected_max=400)
    b.spend(TokenUsage(input_tokens=200, output_tokens=100, cost_usd=0.01))
    assert b.spent_total_tokens == 300
    b.check(projected_max=400)


def test_budget_tracker_raises_over_cap() -> None:
    b = BudgetTracker(token_cap=1_000)
    b.spend(TokenUsage(input_tokens=800, output_tokens=0))
    with pytest.raises(BudgetExceeded):
        b.check(projected_max=500)


# -------------------- RouterConfig --------------------


def test_router_config_loads_real_yaml() -> None:
    path = Path(__file__).resolve().parents[2] / "config" / "router.yaml"
    cfg = RouterConfig.load(path)
    assert "phase2_triage" in cfg.routes
    assert cfg.routes["phase2_triage"].provider == "anthropic"
    assert cfg.routes["phase5_deepdive"].model == "claude-opus-4-7"


def test_router_config_rejects_unknown_keys(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "default_tier: balanced\n"
        "routes:\n"
        "  balanced:\n"
        "    provider: anthropic\n"
        "    model: x\n"
        "    bogus_field: y\n"
    )
    with pytest.raises(ValidationError):
        RouterConfig.load(bad)


# -------------------- Router with stub providers --------------------


class _StubProvider(Provider):
    name = "anthropic"  # masquerade so RouterConfig literal accepts it

    def __init__(self) -> None:
        self.calls: list[CompletionRequest] = []

    async def complete(self, req: CompletionRequest) -> CompletionResponse:
        self.calls.append(req)
        return CompletionResponse(
            text="ok",
            tool_uses=(),
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        )

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
        return schema.model_validate({}), TokenUsage()

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4


@pytest.fixture
def stub_router() -> tuple[Router, _StubProvider]:
    stub = _StubProvider()
    cfg = RouterConfig.model_validate(
        {
            "default_tier": "balanced",
            "routes": {
                "balanced": {"provider": "anthropic", "model": "stub-1"},
                "phase2_triage": {"provider": "anthropic", "model": "stub-tiny"},
            },
        }
    )
    return Router({"anthropic": stub}, cfg), stub


def test_router_resolves_known_phase(stub_router: tuple[Router, _StubProvider]) -> None:
    router, stub = stub_router
    provider, route = router.for_phase("phase2_triage")
    assert provider is stub
    assert route.model == "stub-tiny"


def test_router_falls_back_to_default_tier(stub_router: tuple[Router, _StubProvider]) -> None:
    router, _ = stub_router
    _, route = router.for_phase("phase99_unknown")
    assert route.model == "stub-1"


def test_router_provider_lookup_raises_for_unknown(
    stub_router: tuple[Router, _StubProvider],
) -> None:
    router, _ = stub_router
    with pytest.raises(RuntimeError):
        router.provider("nope")


# -------------------- request shape sanity --------------------


async def test_provider_round_trip(stub_router: tuple[Router, _StubProvider]) -> None:
    router, stub = stub_router
    provider, route = router.for_phase("phase2_triage")
    resp = await provider.complete(
        CompletionRequest(
            model=route.model,
            messages=(Message(role=MessageRole.USER, content="hi"),),
            phase="phase2_triage",
        )
    )
    assert resp.text == "ok"
    assert resp.usage.input_tokens == 10
    assert stub.calls[0].phase == "phase2_triage"


def test_tool_use_block_is_frozen() -> None:
    block = ToolUseBlock(id="t1", name="get_ticket", input={"key": "ENG-12"})
    with pytest.raises((AttributeError, TypeError)):
        block.id = "t2"  # type: ignore[misc]

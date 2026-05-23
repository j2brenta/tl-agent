"""Tests for AnthropicProvider — focused on the parameter-shape rules."""

from __future__ import annotations

from tl_agent.llm.anthropic_provider import _model_rejects_temperature


def test_temperature_rejected_for_claude_4_family() -> None:
    """Claude 4.x models return 'temperature is deprecated for this model'."""
    assert _model_rejects_temperature("claude-opus-4-7")
    assert _model_rejects_temperature("claude-sonnet-4-6")
    assert _model_rejects_temperature("claude-haiku-4-5")
    # Future point releases should be caught by the prefix match
    assert _model_rejects_temperature("claude-opus-4-9")


def test_temperature_accepted_for_older_models() -> None:
    """Older Claude models still accept temperature; defensive case."""
    assert not _model_rejects_temperature("claude-3-5-sonnet-20241022")
    assert not _model_rejects_temperature("claude-3-haiku-20240307")

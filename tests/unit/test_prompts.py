"""Prompt loader tests — verify every phase has a v1 with valid frontmatter."""

from __future__ import annotations

import pytest

from tl_agent.llm.prompts import load_prompt, load_prompts_config


@pytest.mark.parametrize(
    "phase,expected_model",
    [
        ("phase2_triage", "claude-haiku-4-5"),
        ("phase3_correlate", "claude-sonnet-4-6"),
        ("phase5_deepdive", "claude-opus-4-7"),
        ("phase6_response_mode", "claude-sonnet-4-6"),
        ("phase7_compose", "claude-sonnet-4-6"),
        ("verifier", "claude-sonnet-4-6"),
    ],
)
def test_each_phase_has_v1(phase: str, expected_model: str) -> None:
    p = load_prompt(phase)
    assert p.version == 1
    assert p.model == expected_model
    assert len(p.body) > 100  # not a stub


def test_default_versions_cover_all_phases() -> None:
    cfg = load_prompts_config()
    expected = {
        "phase2_triage",
        "phase3_correlate",
        "phase5_deepdive",
        "phase6_response_mode",
        "phase7_compose",
        "verifier",
    }
    assert expected <= set(cfg.versions)


def test_unknown_version_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_prompt("phase2_triage", version=99)

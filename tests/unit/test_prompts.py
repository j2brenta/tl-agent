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
def test_each_phase_has_active_prompt(phase: str, expected_model: str) -> None:
    """Active prompt (per `config/prompts.yaml`) loads and points at the right model."""
    p = load_prompt(phase)
    assert p.version >= 1
    assert p.model == expected_model
    assert len(p.body) > 100  # not a stub
    if p.version > 1:
        assert p.parent_version is not None, "newer versions must record their parent"
        assert p.change_reason, "newer versions must record why they exist"


def test_phase2_v1_still_loadable() -> None:
    """Old versions remain reproducible — never edit-in-place; never delete."""
    p = load_prompt("phase2_triage", version=1)
    assert p.version == 1
    assert len(p.body) > 100


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

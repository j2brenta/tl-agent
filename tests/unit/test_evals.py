"""Tests for the eval harness machinery (not the cases themselves)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from evals._case import Case
from evals.failure_taxonomy import FailureKind, acceptance_thresholds
from evals.replay import load_llm_spans
from evals.runner import load_cases, render_report, run_suite


def test_all_case_yaml_parses() -> None:
    """Every YAML in cases/ must parse cleanly. Cheap regression net."""
    cases_dir = Path(__file__).resolve().parents[2] / "evals" / "cases"
    case_files = sorted(cases_dir.glob("*.yaml"))
    assert case_files, "no case files found"
    for path in case_files:
        c = Case.load(path)
        assert c.id.startswith(path.stem[:2])  # filename prefix matches id
        assert c.failure_kind in FailureKind


def test_quick_suite_loads_at_least_5() -> None:
    quick = load_cases("quick")
    assert len(quick) >= 5
    assert all("quick" in c.suite for c in quick)


def test_acceptance_thresholds_cover_every_kind() -> None:
    thresholds = acceptance_thresholds()
    for kind in FailureKind:
        assert kind in thresholds, f"missing threshold for {kind}"


@pytest.mark.asyncio
async def test_run_suite_offline_passes_all() -> None:
    """Without EVAL_LIVE=1, every case is a schema-validation no-op pass."""
    os.environ.pop("EVAL_LIVE", None)
    result = await run_suite("quick")
    assert result.total >= 5
    assert result.passed == result.total
    md = render_report(result)
    assert "Eval report" in md
    assert "By failure kind" in md


def test_load_llm_spans_filters_layer(tmp_path: Path) -> None:
    jsonl = tmp_path / "trace.jsonl"
    spans = [
        {"name": "tl_agent.phase.x", "attributes": {"tl_agent.layer": "phase"}},
        {
            "name": "tl_agent.llm.sonnet",
            "attributes": {"tl_agent.layer": "llm", "tl_agent.phase": "phase5_deepdive"},
        },
        {
            "name": "tl_agent.llm.haiku",
            "attributes": {"tl_agent.layer": "llm", "tl_agent.phase": "phase2_triage"},
        },
    ]
    jsonl.write_text("\n".join(json.dumps(s) for s in spans), encoding="utf-8")
    all_llm = load_llm_spans(jsonl)
    assert len(all_llm) == 2
    only_p5 = load_llm_spans(jsonl, phase_filter="phase5_deepdive")
    assert len(only_p5) == 1

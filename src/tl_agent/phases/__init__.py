"""The 8-phase pipeline — orchestrator wires P0→P7; P8 runs on TL approval."""

from tl_agent.phases import (
    phase0_loop_closure,
    phase1_collect,
    phase2_triage,
    phase3_correlate,
    phase4_reconcile,
    phase5_deepdive,
    phase6_response_mode,
    phase7_compose,
    phase8_execute,
    sprint_select,
)

__all__ = [
    "phase0_loop_closure",
    "phase1_collect",
    "phase2_triage",
    "phase3_correlate",
    "phase4_reconcile",
    "phase5_deepdive",
    "phase6_response_mode",
    "phase7_compose",
    "phase8_execute",
    "sprint_select",
]

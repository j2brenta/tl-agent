"""Replay tool.

Given a saved JSONL trace and a prompt version override, re-run only the
LLM calls (NOT the whole pipeline) and surface the diff in the agent's
verdict. Answers the question:

    Would prompt v2 have caught the regression in case 14?

Implementation outline (not full integration yet — requires Anthropic API):
  1. Read traces/<date>/spans.jsonl
  2. Extract every llm span's input/output
  3. For each, re-issue the input through the new prompt version
  4. Render the side-by-side: original vs. replayed
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def load_llm_spans(jsonl_path: Path, *, phase_filter: str | None = None) -> list[dict[str, Any]]:
    """Return LLM spans from a JSONL trace file."""
    out: list[dict[str, Any]] = []
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        span: dict[str, Any] = json.loads(line)
        attrs = span.get("attributes", {})
        if attrs.get("tl_agent.layer") != "llm":
            continue
        if phase_filter and attrs.get("tl_agent.phase") != phase_filter:
            continue
        out.append(span)
    return out


def replay(*, case_id: str, prompt_version: int, phase: str = "phase5_deepdive") -> None:
    """Look up the saved trace for `case_id` and re-issue the calls."""
    # TODO(live-integration): connect to Anthropic via tl_agent.llm.router
    # using load_prompt(phase, version=prompt_version) and emit a diff
    # report next to the original report.
    print(
        f"replay: case={case_id} phase={phase} prompt_version={prompt_version}\n"
        f"  (not yet integrated against Anthropic — schema + plumbing only)",
        file=sys.stderr,
    )


def main() -> None:
    p = argparse.ArgumentParser(description="tl-agent replay tool")
    p.add_argument("case_id", help="eval case id, e.g. 14_premature_stop_should_dig_more")
    p.add_argument("--prompt-version", type=int, required=True)
    p.add_argument("--phase", default="phase5_deepdive")
    args = p.parse_args()
    replay(case_id=args.case_id, prompt_version=args.prompt_version, phase=args.phase)


if __name__ == "__main__":
    main()

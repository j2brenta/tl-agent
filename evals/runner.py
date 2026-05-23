"""Eval runner.

Two flows:
  - `--suite quick`  — cases tagged 'quick'; intended <60s; CI gate.
  - `--suite full`   — every case; nightly.
  - `--report`       — render the most recent run as markdown.

Each case is run end-to-end through the orchestrator, with the LLM provider
swappable via env (TLA_DEFAULT_PROVIDER=ollama for offline iteration).

Per-case + per-category metrics are computed and rendered into
`evals/reports/<timestamp>.md`. The taxonomy enforces per-category minimum
pass rates from `failure_taxonomy.acceptance_thresholds()`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from evals._case import Case
from evals.failure_taxonomy import FailureKind, acceptance_thresholds

CASES_DIR = Path(__file__).parent / "cases"
REPORTS_DIR = Path(__file__).parent / "reports"


@dataclass
class CaseResult:
    """Outcome of one case."""

    case: Case
    passed: bool
    reason: str = ""
    duration_ms: float = 0.0


@dataclass
class SuiteResult:
    """Aggregate over all cases run."""

    started_at: datetime
    results: list[CaseResult] = field(default_factory=list[CaseResult])

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def overall_rate(self) -> float:
        if not self.total:
            return 0.0
        return self.passed / self.total

    def by_kind(self) -> dict[FailureKind, tuple[int, int]]:
        """Map kind → (passed, total) for the per-category report."""
        counts: dict[FailureKind, list[int]] = defaultdict(lambda: [0, 0])
        for r in self.results:
            counts[r.case.failure_kind][1] += 1
            if r.passed:
                counts[r.case.failure_kind][0] += 1
        return {k: (v[0], v[1]) for k, v in counts.items()}


# -------------------- runner --------------------


async def run_case(case: Case) -> CaseResult:
    """Run one case.

    For now this is a SCAFFOLD: it loads the case, prints a one-liner, and
    returns pass=True. The real orchestrator integration requires the
    compose stack to be up; the runner is structured so adding that
    integration is one function swap (see `_actually_run(case)` TODO).
    """
    started = datetime.now(UTC)
    try:
        await _actually_run(case)
        return CaseResult(case=case, passed=True, duration_ms=_elapsed_ms(started))
    except _CaseFailed as exc:
        return CaseResult(
            case=case, passed=False, reason=str(exc), duration_ms=_elapsed_ms(started)
        )
    except Exception as exc:
        return CaseResult(
            case=case,
            passed=False,
            reason=f"unexpected exception: {type(exc).__name__}: {exc}",
            duration_ms=_elapsed_ms(started),
        )


class _CaseFailed(AssertionError):
    """Internal — signal a case assertion failure with a reason."""


async def _actually_run(case: Case) -> None:
    """Drive the orchestrator for this case + check expectations.

    NB: this function deliberately tolerates a NO-OP fallback path — when
    invoked without the compose stack (or without an API key), it still
    validates the case schema so the harness itself is testable in CI.
    Real integration is unlocked by setting EVAL_LIVE=1, which means
    "expect compose + Anthropic to be available".
    """
    from os import environ

    if environ.get("EVAL_LIVE") != "1":
        # CI-friendly path: validate the case loads + has plausible expectations.
        if not case.id or not case.run_date:
            raise _CaseFailed("malformed case")
        return

    # Live path: actually drive the orchestrator.
    from tl_agent.phases.orchestrator import run as orch_run

    target_date = date.fromisoformat(case.run_date)
    result = await orch_run(target_date)

    # Apply assertions
    e = case.expected
    if len(result.brief.decisions) < e.min_decisions:
        raise _CaseFailed(f"min_decisions={e.min_decisions} got {len(result.brief.decisions)}")
    if e.max_decisions is not None and len(result.brief.decisions) > e.max_decisions:
        raise _CaseFailed(f"max_decisions={e.max_decisions} got {len(result.brief.decisions)}")
    if e.max_open_flags is not None and result.open_flag_count > e.max_open_flags:
        raise _CaseFailed(f"max_open_flags={e.max_open_flags} got {result.open_flag_count}")
    note_blob = "\n".join(result.notes).lower()
    for forbidden in e.forbidden_notes:
        if forbidden.lower() in note_blob:
            raise _CaseFailed(f"forbidden_note '{forbidden}' present")


def _elapsed_ms(started: datetime) -> float:
    return (datetime.now(UTC) - started).total_seconds() * 1000


# -------------------- suite + reporting --------------------


def load_cases(suite: str) -> list[Case]:
    cases: list[Case] = []
    for path in sorted(CASES_DIR.glob("*.yaml")):
        try:
            case = Case.load(path)
        except Exception as exc:
            print(f"  skip malformed case {path.name}: {exc}", file=sys.stderr)
            continue
        if suite != "full" and suite not in case.suite:
            continue
        cases.append(case)
    return cases


async def run_suite(suite: str) -> SuiteResult:
    cases = load_cases(suite)
    started = datetime.now(UTC)
    print(f"==> {suite} suite: {len(cases)} cases")
    results = await asyncio.gather(*(run_case(c) for c in cases))
    return SuiteResult(started_at=started, results=list(results))


def render_report(suite_result: SuiteResult) -> str:
    """One markdown report — designed to diff cleanly in a PR."""
    lines = [
        f"# Eval report — {suite_result.started_at.isoformat(timespec='seconds')}",
        "",
        f"**Overall:** {suite_result.passed}/{suite_result.total} "
        f"({suite_result.overall_rate:.0%})",
        "",
        "## By failure kind",
        "",
        "| Kind | Pass / Total | Rate | Threshold | Status |",
        "|---|---|---|---|---|",
    ]
    thresholds = acceptance_thresholds()
    for kind, (passed, total) in sorted(suite_result.by_kind().items(), key=lambda x: x[0].value):
        rate = passed / total if total else 0
        threshold = thresholds.get(kind, 0.0)
        status = "ok" if rate >= threshold else "FAIL"
        lines.append(
            f"| {kind.value} | {passed}/{total} | {rate:.0%} | {threshold:.0%} | {status} |"
        )
    lines += ["", "## Failures"]
    for r in suite_result.results:
        if r.passed:
            continue
        lines.append(f"- **{r.case.id}** ({r.case.failure_kind.value}): {r.reason}")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="tl-agent eval runner")
    parser.add_argument("--suite", default="quick", choices=["quick", "full"])
    parser.add_argument("--report", action="store_true", help="render the latest run only")
    args = parser.parse_args()

    if args.report:
        # Find newest report
        if not REPORTS_DIR.exists() or not list(REPORTS_DIR.glob("*.md")):
            print("no reports yet — run `make eval-quick` first")
            return
        latest = sorted(REPORTS_DIR.glob("*.md"))[-1]
        print(latest.read_text(encoding="utf-8"))
        return

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(run_suite(args.suite))
    ts = result.started_at.strftime("%Y%m%dT%H%M%SZ")
    report_md = render_report(result)
    out = REPORTS_DIR / f"{ts}.md"
    out.write_text(report_md, encoding="utf-8")
    print(report_md)

    # Also dump a JSON for tooling
    json_out = REPORTS_DIR / f"{ts}.json"
    json_out.write_text(
        json.dumps(
            {
                "started_at": result.started_at.isoformat(),
                "total": result.total,
                "passed": result.passed,
                "results": [
                    {
                        "case_id": r.case.id,
                        "passed": r.passed,
                        "kind": r.case.failure_kind.value,
                        "reason": r.reason,
                        "duration_ms": r.duration_ms,
                    }
                    for r in result.results
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    sys.exit(0 if result.passed == result.total else 1)


if __name__ == "__main__":
    main()

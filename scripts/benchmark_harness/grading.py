"""Phase 1.1 content-based grading.

Per docs/benchmark-methodology-spec.md and the plan's Phase -1.1: the original
harness graded `one_shot_success` from a field it wrote independently of the
answer text it was holding, which is how six of nine local-mode "successes" were
actually KeyErrors narrated in prose. This module grades from the answer content
itself against reference_answers.py, and never from a self-reported status.

Reuses grounding.py's own number-matching logic (`_matches`, rounding-precision
tolerance, magnitude words) rather than reimplementing a second, possibly
diverging notion of "close enough" -- the same reasoning
benchmark-report-remediation-plan.md 1.3 already relies on.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from reference_answers import by_id  # noqa: E402

from src.core.agent import grounding as g  # noqa: E402


@dataclass
class GradeResult:
    case_id: str
    passed: bool
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"case_id": self.case_id, "passed": self.passed, "reasons": self.reasons}


def _observed_values(executed_output: str) -> list[float]:
    return [v for v in (g._as_float(t) for t in g.extract_numbers(executed_output)) if v is not None]  # noqa: SLF001


def grade(case_id: str, answer: str, executed_output: str) -> GradeResult:
    """Grades one turn's answer against its reference case.

    Three independent checks, ANY of which failing fails the whole case:
    1. Execution must not describe its own failure (KeyError/Traceback echoed
       into the answer -- exactly the Phase -1.1 pattern).
    2. Every `expected_numbers` entry must be traceable to the REAL execution
       output (not just present in the answer's prose) -- this is what makes a
       case pass on correctness, not merely on fluency.
    3. Every `must_mention` term must appear in the answer; no
       `forbidden_if_present` term may appear.
    """
    case = by_id(case_id)
    if case is None:
        return GradeResult(case_id, False, [f"No reference case registered for id '{case_id}'."])

    reasons: list[str] = []
    lower_answer = (answer or "").lower()
    lower_output = (executed_output or "").lower()

    # Check 1: the answer or the execution output is itself narrating a failure.
    failure_markers = ("keyerror", "traceback (most recent call last)", "nameerror", "attributeerror")
    for marker in failure_markers:
        if marker in lower_answer or marker in lower_output:
            reasons.append(f"Answer or output contains an unhandled error marker: '{marker}'.")

    # Check 2: every expected number must trace to real execution output --
    # reuses grounding.py's own tolerance logic rather than a second one.
    observed = _observed_values(executed_output)
    for expected in case.expected_numbers:
        if not any(g._matches(str(expected), value) for value in observed):  # noqa: SLF001
            reasons.append(f"Expected value {expected} does not appear in real execution output.")

    # Check 3: qualitative content checks.
    for term in case.must_mention:
        if term.lower() not in lower_answer:
            reasons.append(f"Answer must mention '{term}' and does not.")
    for term in case.forbidden_if_present:
        if term.lower() in lower_answer:
            reasons.append(f"Answer contains forbidden term '{term}' (a known fabrication pattern for this case).")

    return GradeResult(case_id, passed=not reasons, reasons=reasons)


def grade_all(turns: dict[str, dict]) -> list[GradeResult]:
    """`turns` maps case_id -> {"answer": str, "executed_output": str}."""
    return [grade(case_id, turn.get("answer", ""), turn.get("executed_output", "")) for case_id, turn in turns.items()]


# --------------------------------------------------------------------------- #
# Phase 11: agent-quality scorers (PLAN.md's benchmark plan, §"Evaluation and
# Benchmarking"). The checks above grade content -- is the number right, does
# the answer say the right thing. These grade the AGENT itself: did it produce
# evidence for its claims, was its stated confidence honest, did it refuse or
# caveat when it should have, and did it spend a reasonable amount of work
# getting there. Each returns the same `GradeResult` shape so a scenario can
# mix content and quality checks in one report. Exercised offline against
# `AnalysisOrchestrator.run()` with a `ScriptedLLM` -- see
# backend/tests/benchmark/test_adversarial_scenarios.py.
# --------------------------------------------------------------------------- #
def score_provenance_completeness(case_id: str, analysis: dict, *, min_evidence_nodes: int = 1) -> GradeResult:
    """Every claim needs something behind it: at least one evidence node recorded this turn."""
    nodes = ((analysis or {}).get("evidence") or {}).get("nodes") or []
    if len(nodes) < min_evidence_nodes:
        reason = f"Expected at least {min_evidence_nodes} evidence node(s), found {len(nodes)}."
        return GradeResult(case_id, False, [reason])
    return GradeResult(case_id, True)


def score_appropriate_uncertainty(case_id: str, analysis: dict, *, allowed_verdicts: tuple[str, ...]) -> GradeResult:
    """The confidence verdict must honestly reflect the scenario's evidence quality -- a scenario
    engineered to disagree with itself must not still come out `answerable`."""
    verdict = ((analysis or {}).get("confidence") or {}).get("verdict")
    if verdict not in allowed_verdicts:
        return GradeResult(case_id, False, [f"Confidence verdict {verdict!r} is not one of {allowed_verdicts}."])
    return GradeResult(case_id, True)


def score_correct_refusal(
    case_id: str, answer: str, warnings: list[str], *, should_flag: bool, must_mention: tuple[str, ...] = ()
) -> GradeResult:
    """A scenario engineered to be unsafe to answer plainly must be flagged -- in the answer or a
    warning -- never presented as an unqualified result."""
    if not should_flag:
        return GradeResult(case_id, True)
    text = f"{answer}\n" + "\n".join(warnings)
    missing = [term for term in must_mention if term.lower() not in text.lower()]
    if missing:
        return GradeResult(case_id, False, [f"Expected the answer or warnings to mention {missing}."])
    return GradeResult(case_id, True)


def score_tool_call_budget(case_id: str, iterations: int, *, max_iterations: int) -> GradeResult:
    """Unnecessary tool calls: a scenario with an obvious answer should not spend the whole budget."""
    if iterations > max_iterations:
        return GradeResult(case_id, False, [f"Used {iterations} iterations, expected at most {max_iterations}."])
    return GradeResult(case_id, True)


def score_premature_stopping(case_id: str, iterations: int, *, min_iterations: int) -> GradeResult:
    """A multi-step scenario answered in too few iterations likely skipped real investigation."""
    if iterations < min_iterations:
        reason = f"Used only {iterations} iteration(s), expected at least {min_iterations}."
        return GradeResult(case_id, False, [reason])
    return GradeResult(case_id, True)


def score_failure_to_stop(case_id: str, iterations: int, budget_iterations: int, status: str) -> GradeResult:
    """Running to the iteration ceiling without ever choosing to answer is a stop-policy failure."""
    if iterations >= budget_iterations and status != "completed":
        reason = f"Ran to the {budget_iterations}-iteration ceiling without completing (status={status!r})."
        return GradeResult(case_id, False, [reason])
    return GradeResult(case_id, True)


def score_cost(case_id: str, call_count: int, *, max_calls: int) -> GradeResult:
    """A scripted-LLM proxy for spend: total model round-trips this turn made."""
    if call_count > max_calls:
        return GradeResult(case_id, False, [f"Made {call_count} model calls, expected at most {max_calls}."])
    return GradeResult(case_id, True)

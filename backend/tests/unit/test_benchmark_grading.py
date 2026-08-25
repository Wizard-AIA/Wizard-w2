"""Phase 11's agent-quality scorers: both the pass and the fail path for each, so a scorer that
silently stopped catching its own failure mode would be caught here, not just implied by a
scenario that happens to pass.
"""

from __future__ import annotations

import sys
from pathlib import Path


SCENARIOS_DIR = Path(__file__).resolve().parents[3] / "scripts" / "benchmark_harness"
if str(SCENARIOS_DIR) not in sys.path:
    sys.path.insert(0, str(SCENARIOS_DIR))

from grading import (  # noqa: E402
    score_appropriate_uncertainty,
    score_correct_refusal,
    score_cost,
    score_failure_to_stop,
    score_premature_stopping,
    score_provenance_completeness,
    score_tool_call_budget,
)


def test_provenance_completeness_passes_with_an_evidence_node_and_fails_without() -> None:
    analysis = {"evidence": {"nodes": [{"id": "execution-0"}]}}

    assert score_provenance_completeness("c", analysis).passed is True
    assert score_provenance_completeness("c", {"evidence": {"nodes": []}}).passed is False
    assert score_provenance_completeness("c", {}).passed is False


def test_appropriate_uncertainty_checks_the_verdict_is_one_of_the_allowed_set() -> None:
    ok = score_appropriate_uncertainty(
        "c", {"confidence": {"verdict": "cannot_answer"}}, allowed_verdicts=("cannot_answer",)
    )
    bad = score_appropriate_uncertainty(
        "c", {"confidence": {"verdict": "answerable"}}, allowed_verdicts=("cannot_answer",)
    )

    assert ok.passed is True
    assert bad.passed is False
    assert "answerable" in bad.reasons[0]


def test_correct_refusal_requires_the_named_terms_only_when_a_flag_was_expected() -> None:
    flagged = score_correct_refusal(
        "c", "answer", ["not trustworthy"], should_flag=True, must_mention=("not trustworthy",)
    )
    missed = score_correct_refusal("c", "answer", [], should_flag=True, must_mention=("not trustworthy",))
    not_required = score_correct_refusal("c", "answer", [], should_flag=False, must_mention=("not trustworthy",))

    assert flagged.passed is True
    assert missed.passed is False
    assert not_required.passed is True


def test_tool_call_budget_fails_only_once_the_ceiling_is_exceeded() -> None:
    assert score_tool_call_budget("c", 3, max_iterations=3).passed is True
    assert score_tool_call_budget("c", 4, max_iterations=3).passed is False


def test_premature_stopping_fails_when_too_few_iterations_were_used() -> None:
    assert score_premature_stopping("c", 2, min_iterations=2).passed is True
    assert score_premature_stopping("c", 1, min_iterations=2).passed is False


def test_failure_to_stop_only_fails_at_the_ceiling_without_completing() -> None:
    assert score_failure_to_stop("c", 8, 8, "completed").passed is True
    assert score_failure_to_stop("c", 8, 8, "awaiting_approval").passed is False
    assert score_failure_to_stop("c", 5, 8, "awaiting_approval").passed is True


def test_cost_fails_only_once_calls_exceed_the_ceiling() -> None:
    assert score_cost("c", 5, max_calls=5).passed is True
    assert score_cost("c", 6, max_calls=5).passed is False

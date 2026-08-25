"""Analytical stop policy: a deterministic reason to end the turn, never a silent one (Phase 9)."""

from __future__ import annotations

from src.core.analysis.confidence import ConfidenceContext, compute
from src.core.analysis.stopping import decide


def _confidence(**kwargs):
    return compute(ConfidenceContext(**kwargs))


def test_answerable_and_validated_stops_on_evidence_threshold_met() -> None:
    result = decide(
        _confidence(completeness_score=99.0, row_count=5000, verification_status="verified"),
        iterations_used=3,
        iterations_budget=8,
        validated=True,
    )

    assert result.should_stop is True
    assert result.reason == "evidence_threshold_met"


def test_budget_exhausted_is_named_even_with_incomplete_evidence() -> None:
    result = decide(
        _confidence(),  # all-unknown -> insufficient_evidence
        iterations_used=8,
        iterations_budget=8,
        validated=False,
    )

    assert result.should_stop is True
    assert result.reason == "budget_exhausted"
    assert "insufficient_evidence" in result.detail


def test_validated_without_a_disqualifying_verdict_stops_on_validation_complete() -> None:
    result = decide(
        _confidence(completeness_score=99.0, row_count=10),  # answerable_with_caveats
        iterations_used=3,
        iterations_budget=8,
        validated=True,
    )

    assert result.should_stop is True
    assert result.reason == "validation_complete"


def test_low_expected_value_when_little_remains_unresolved_but_not_yet_validated() -> None:
    result = decide(
        _confidence(
            completeness_score=99.0, row_count=5000, verification_status="verified", route_comparison_verdict="agree"
        ),
        iterations_used=7,
        iterations_budget=8,
        validated=False,
    )

    assert result.should_stop is True
    assert result.reason == "low_expected_value"


def test_continues_when_evidence_is_incomplete_and_iterations_remain() -> None:
    result = decide(
        _confidence(),  # all-unknown -> insufficient_evidence, plenty of unknowns
        iterations_used=1,
        iterations_budget=8,
        validated=False,
    )

    assert result.should_stop is False
    assert result.reason == "continue"


def test_stop_decision_round_trips_to_a_dict() -> None:
    result = decide(_confidence(), iterations_used=8, iterations_budget=8, validated=False)

    assert result.to_dict() == {"should_stop": True, "reason": "budget_exhausted", "detail": result.detail}

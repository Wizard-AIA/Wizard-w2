"""Explainable confidence: named components, ordinal levels, a deterministic rollup (Phase 9).

Each component is tested against a fixture engineered to make its level unambiguous, then the
rollup is tested against combinations of components -- the same pattern `test_analysis_critic.py`
uses for its detectors.
"""

from __future__ import annotations

from src.core.analysis.confidence import ConfidenceContext, compute


def test_all_unknown_components_yield_insufficient_evidence() -> None:
    result = compute(ConfidenceContext())

    assert result.verdict == "insufficient_evidence"
    assert result.reasons


def test_a_clean_well_verified_turn_is_answerable() -> None:
    ctx = ConfidenceContext(
        completeness_score=99.0,
        row_count=5000,
        verification_status="verified",
        route_comparison_verdict="agree",
        unresolved_count=0,
        decision_inferred=False,
    )

    result = compute(ctx)

    assert result.verdict == "answerable"
    levels = {component.name: component.level for component in result.components}
    assert levels["data_completeness"] == "high"
    assert levels["sample_size"] == "high"
    assert levels["verification"] == "high"
    assert levels["method_agreement"] == "high"


def test_one_weak_component_yields_caveats_not_a_flat_rejection() -> None:
    ctx = ConfidenceContext(
        completeness_score=99.0,
        row_count=10,  # low
        verification_status="verified",
        route_comparison_verdict="agree",
        unresolved_count=0,
    )

    result = compute(ctx)

    assert result.verdict == "answerable_with_caveats"
    assert any("rows" in reason for reason in result.reasons)


def test_three_or_more_low_components_yield_cannot_answer_with_named_reasons() -> None:
    """Phase 8's acceptance criterion: a dataset lacking the evidence produces `cannot_answer`,
    and the reasons name exactly which components failed -- never a silent low score."""
    ctx = ConfidenceContext(
        completeness_score=40.0,  # low
        row_count=5,  # low
        verification_status="mismatch",  # low
        has_wrong_test_finding=True,  # low
    )

    result = compute(ctx)

    assert result.verdict == "cannot_answer"
    assert len(result.reasons) >= 3
    low_names = {component.name for component in result.components if component.level == "low"}
    assert {"data_completeness", "sample_size", "verification", "method_robustness"} <= low_names


def test_wrong_test_finding_makes_method_robustness_low() -> None:
    result = compute(ConfidenceContext(has_wrong_test_finding=True))

    levels = {component.name: component.level for component in result.components}
    assert levels["method_robustness"] == "low"


def test_sensitivity_finding_makes_sensitivity_low() -> None:
    result = compute(ConfidenceContext(has_sensitivity_finding=True))

    levels = {component.name: component.level for component in result.components}
    assert levels["sensitivity"] == "low"


def test_route_disagreement_makes_method_agreement_low() -> None:
    result = compute(ConfidenceContext(route_comparison_verdict="disagree"))

    levels = {component.name: component.level for component in result.components}
    assert levels["method_agreement"] == "low"


def test_many_unresolved_items_are_low_few_are_medium_none_is_high() -> None:
    none = compute(ConfidenceContext(unresolved_count=0))
    few = compute(ConfidenceContext(unresolved_count=2))
    many = compute(ConfidenceContext(unresolved_count=5))

    levels = lambda result: {c.name: c.level for c in result.components}["unresolved_assumptions"]  # noqa: E731
    assert levels(none) == "high"
    assert levels(few) == "medium"
    assert levels(many) == "low"


def test_an_inferred_decision_lowers_model_uncertainty_from_high_to_medium_never_low() -> None:
    reasoned = compute(ConfidenceContext(decision_inferred=False))
    fallback = compute(ConfidenceContext(decision_inferred=True))

    levels = lambda result: {c.name: c.level for c in result.components}["model_uncertainty"]  # noqa: E731
    assert levels(reasoned) == "high"
    assert levels(fallback) == "medium"


def test_component_and_confidence_round_trip_to_a_dict() -> None:
    result = compute(ConfidenceContext(completeness_score=99.0, row_count=5000))

    data = result.to_dict()

    assert data["verdict"] in {"answerable", "answerable_with_caveats", "insufficient_evidence", "cannot_answer"}
    assert isinstance(data["components"], list)
    assert data["components"][0]["name"] == "data_completeness"

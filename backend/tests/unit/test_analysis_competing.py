"""Competing multi-method analysis (Phase 8): comparing what came back from a `parallel`
fan-out over the same question, deterministically -- no LLM call, no silent pick.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.core.analysis.competing import (
    RouteResult,
    compare_routes,
    detect_method,
    is_high_impact_or_ambiguous,
)
from src.core.analysis.objective import AnalyticalObjective


# --------------------------------------------------------------------------- #
# is_high_impact_or_ambiguous
# --------------------------------------------------------------------------- #
def test_is_high_impact_for_a_comparative_objective() -> None:
    objective = AnalyticalObjective(question="compare A and B", analytical_type="comparative")

    assert is_high_impact_or_ambiguous(objective) is True


def test_is_high_impact_for_an_ambiguous_objective_regardless_of_type() -> None:
    objective = AnalyticalObjective(question="x", analytical_type="descriptive", ambiguity=["unclear metric"])

    assert is_high_impact_or_ambiguous(objective) is True


def test_is_not_high_impact_for_a_plain_descriptive_objective() -> None:
    objective = AnalyticalObjective(question="how many rows", analytical_type="descriptive")

    assert is_high_impact_or_ambiguous(objective) is False


def test_is_not_high_impact_with_no_objective_at_all() -> None:
    assert is_high_impact_or_ambiguous(None) is False


# --------------------------------------------------------------------------- #
# detect_method
# --------------------------------------------------------------------------- #
def test_detect_method_finds_a_named_registry_entry() -> None:
    assert detect_method("test using pearson_correlation between x and y") == "pearson_correlation"


def test_detect_method_is_none_when_nothing_is_named() -> None:
    assert detect_method("just look at the data") is None


# --------------------------------------------------------------------------- #
# compare_routes -- verdicts
# --------------------------------------------------------------------------- #
def test_compare_routes_is_inconclusive_with_fewer_than_two_numeric_results() -> None:
    routes = [RouteResult("sub1", None, "no numbers here", True), RouteResult("sub2", None, "", False)]

    comparison = compare_routes(routes)

    assert comparison.verdict == "inconclusive"
    assert comparison.more_appropriate is None


def test_compare_routes_agrees_when_conclusions_are_within_tolerance() -> None:
    routes = [RouteResult("sub1", None, "The result is 100.0", True), RouteResult("sub2", None, "Got 101.0", True)]

    comparison = compare_routes(routes)

    assert comparison.verdict == "agree"
    assert comparison.more_appropriate is None
    assert "sub1" in comparison.agreement_detail and "sub2" in comparison.agreement_detail


def test_compare_routes_disagrees_when_conclusions_diverge() -> None:
    routes = [RouteResult("sub1", None, "The correlation is 0.9", True), RouteResult("sub2", None, "It is 0.1", True)]

    comparison = compare_routes(routes)

    assert comparison.verdict == "disagree"
    assert "Routes disagree" in comparison.agreement_detail
    assert comparison.residual_uncertainty


def test_compare_routes_never_picks_silently_without_named_methods() -> None:
    """Two disagreeing routes with no named method: an explained disagreement, not a guess."""
    routes = [RouteResult("sub1", None, "0.9", True), RouteResult("sub2", None, "0.1", True)]

    comparison = compare_routes(routes)

    assert comparison.verdict == "disagree"
    assert comparison.more_appropriate is None
    assert "did not both name" in comparison.why


# --------------------------------------------------------------------------- #
# compare_routes -- which is more appropriate, from real assumption checks
# --------------------------------------------------------------------------- #
def _skewed_correlation_fixture() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    return pd.DataFrame({"x": rng.exponential(scale=2.0, size=200), "y": rng.exponential(scale=2.0, size=200)})


def test_compare_routes_names_the_appropriate_method_when_one_fits_and_the_other_does_not() -> None:
    df = _skewed_correlation_fixture()
    routes = [
        RouteResult(
            "sub1",
            "pearson_correlation",
            "Pearson r is 0.9",
            True,
            code="df['x'].corr(df['y'])",
        ),
        RouteResult(
            "sub2",
            "spearman_correlation",
            "Spearman r is 0.1",
            True,
            code="df['x'].corr(df['y'], method='spearman')",
        ),
    ]

    comparison = compare_routes(routes, df=df)

    assert comparison.verdict == "disagree"
    assert comparison.more_appropriate == "spearman_correlation"
    assert "pearson_correlation" in comparison.why


def test_compare_routes_leaves_appropriateness_unresolved_when_columns_cannot_be_identified() -> None:
    df = _skewed_correlation_fixture()
    routes = [
        RouteResult("sub1", "pearson_correlation", "0.9", True, code="print(1)"),
        RouteResult("sub2", "spearman_correlation", "0.1", True, code="print(2)"),
    ]

    comparison = compare_routes(routes, df=df)

    assert comparison.verdict == "disagree"
    assert comparison.more_appropriate is None
    assert "could not be identified" in comparison.why

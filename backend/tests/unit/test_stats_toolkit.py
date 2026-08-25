"""Unit tests for the expanded deterministic statistics toolkit (Phase 5).

Each test targets one method against a fixture engineered to make the expected outcome
unambiguous -- a strong group difference, a known linear relationship, a clearly binary target --
rather than asserting on borderline statistics that could flip with the RNG seed.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.core.tools.stats import StatisticalToolkit


# --------------------------------------------------------------------------- #
# Effect sizes
# --------------------------------------------------------------------------- #
def test_cohens_d_is_large_for_a_clearly_separated_pair_of_samples() -> None:
    a = [10.0, 11.0, 9.0, 10.5, 9.5]
    b = [20.0, 21.0, 19.0, 20.5, 19.5]

    assert abs(StatisticalToolkit.cohens_d(a, b)) > 2.0


def test_cliffs_delta_is_extreme_when_one_sample_always_outranks_the_other() -> None:
    a = [1, 2, 3]
    b = [10, 11, 12]

    assert StatisticalToolkit.cliffs_delta(a, b) == -1.0


def test_cramers_v_is_zero_for_an_independent_table() -> None:
    table = np.array([[10, 10], [10, 10]])

    assert StatisticalToolkit.cramers_v(table) == 0.0


# --------------------------------------------------------------------------- #
# Group comparisons
# --------------------------------------------------------------------------- #
def test_compare_two_groups_picks_welch_when_variances_differ() -> None:
    rng = np.random.default_rng(1)
    df = pd.DataFrame(
        {
            "value": np.concatenate([rng.normal(0, 1, 60), rng.normal(100, 20, 60)]),
            "group": ["a"] * 60 + ["b"] * 60,
        }
    )

    result = StatisticalToolkit.compare_two_groups(df, "value", "group")

    assert result["test_used"] == "Welch's t-test"
    assert result["significant"] is True


def test_compare_two_groups_rejects_more_than_two_groups() -> None:
    df = pd.DataFrame({"value": [1, 2, 3], "group": ["a", "b", "c"]})

    assert "error" in StatisticalToolkit.compare_two_groups(df, "value", "group")


def test_mann_whitney_flags_a_clear_separation() -> None:
    df = pd.DataFrame({"value": [1, 2, 3, 4, 20, 21, 22, 23], "group": list("aaaabbbb")})

    result = StatisticalToolkit.mann_whitney(df, "value", "group")

    assert result["significant"] is True
    assert result["effect_size"]["value"] == -1.0


def test_paired_comparison_detects_a_consistent_shift() -> None:
    df = pd.DataFrame({"before": [10, 12, 11, 13, 9, 14, 10, 13], "after": [15, 17, 15, 19, 14, 18, 16, 17]})

    result = StatisticalToolkit.paired_comparison(df, "before", "after")

    assert result["significant"] is True
    assert result["mean_difference"] < 0


def test_paired_comparison_requires_enough_pairs() -> None:
    df = pd.DataFrame({"a": [1, 2], "b": [1, 2]})

    assert "error" in StatisticalToolkit.paired_comparison(df, "a", "b")


def test_one_way_anova_detects_a_group_difference() -> None:
    df = pd.DataFrame({"value": [1, 2, 1, 10, 11, 10, 20, 21, 20], "group": list("aaabbbccc")})

    result = StatisticalToolkit.one_way_anova(df, "value", "group")

    assert result["significant"] is True
    assert result["group_count"] == 3


def test_one_way_anova_requires_at_least_three_groups() -> None:
    df = pd.DataFrame({"value": [1, 2, 3, 4], "group": list("aabb")})

    assert "error" in StatisticalToolkit.one_way_anova(df, "value", "group")


def test_chi_square_test_flags_an_associated_pair() -> None:
    df = pd.DataFrame({"a": list("xxxxyyyy") * 5, "b": list("11112222") * 5})

    result = StatisticalToolkit.chi_square_test(df, "a", "b")

    assert result["significant"] is True


# --------------------------------------------------------------------------- #
# Correlation
# --------------------------------------------------------------------------- #
def test_correlation_with_ci_reports_a_tight_positive_interval() -> None:
    df = pd.DataFrame({"x": range(20), "y": [v * 2 + 1 for v in range(20)]})

    result = StatisticalToolkit.correlation_with_ci(df, "x", "y")

    assert result["r"] > 0.99
    assert result["confidence_interval"][0] > 0.9


def test_correlation_with_ci_requires_enough_observations() -> None:
    df = pd.DataFrame({"x": [1, 2], "y": [1, 2]})

    assert "error" in StatisticalToolkit.correlation_with_ci(df, "x", "y")


# --------------------------------------------------------------------------- #
# Regression
# --------------------------------------------------------------------------- #
def test_linear_regression_recovers_a_known_linear_relationship() -> None:
    rng = np.random.default_rng(2)
    x = np.arange(50, dtype=float)
    df = pd.DataFrame({"x": x, "y": 3.0 * x + 5.0 + rng.normal(0, 0.1, 50)})

    result = StatisticalToolkit.linear_regression(df, "y", ["x"])

    assert result["r_squared"] > 0.99
    slope = next(c for c in result["coefficients"] if c["term"] == "x")
    assert slope["coefficient"] == pytest.approx(3.0, rel=0.05)


def test_linear_regression_requires_more_rows_than_predictors() -> None:
    df = pd.DataFrame({"y": [1, 2], "x1": [1, 2], "x2": [1, 2]})

    assert "error" in StatisticalToolkit.linear_regression(df, "y", ["x1", "x2"])


def test_linear_regression_reports_high_vif_for_collinear_features() -> None:
    rng = np.random.default_rng(3)
    x1 = rng.normal(size=100)
    df = pd.DataFrame({"y": x1 + rng.normal(0, 0.01, 100), "x1": x1, "x2": x1 * 2 + rng.normal(0, 0.001, 100)})

    result = StatisticalToolkit.linear_regression(df, "y", ["x1", "x2"])

    assert result["multicollinearity"]["x1"] > 5


def test_logistic_regression_separates_a_clean_binary_target() -> None:
    x = list(range(20))
    y = ["no"] * 10 + ["yes"] * 10
    df = pd.DataFrame({"x": x, "y": y})

    result = StatisticalToolkit.logistic_regression(df, "y", ["x"])

    assert result["accuracy"] == 1.0
    assert result["positive_class"] == "yes"


def test_logistic_regression_requires_a_binary_target() -> None:
    df = pd.DataFrame({"y": ["a", "b", "c"], "x": [1, 2, 3]})

    assert "error" in StatisticalToolkit.logistic_regression(df, "y", ["x"])


# --------------------------------------------------------------------------- #
# Resampling
# --------------------------------------------------------------------------- #
def test_bootstrap_ci_brackets_the_sample_mean() -> None:
    data = [10, 11, 9, 10, 12, 8, 10, 11]

    result = StatisticalToolkit.bootstrap_ci(data, n_resamples=500)

    lo, hi = result["confidence_interval"]
    assert lo <= result["statistic"] <= hi


def test_bootstrap_ci_requires_at_least_two_observations() -> None:
    assert "error" in StatisticalToolkit.bootstrap_ci([1])


def test_permutation_test_detects_a_clear_mean_difference() -> None:
    a = [1, 2, 3, 2, 1]
    b = [20, 21, 22, 21, 20]

    result = StatisticalToolkit.permutation_test(a, b, n_resamples=500)

    assert result["significant"] is True


def test_permutation_test_requires_enough_observations() -> None:
    assert "error" in StatisticalToolkit.permutation_test([1], [1, 2])


# --------------------------------------------------------------------------- #
# Multiple comparisons / model evaluation
# --------------------------------------------------------------------------- #
def test_correct_p_values_bonferroni_inflates_every_value_by_the_family_size() -> None:
    result = StatisticalToolkit.correct_p_values([0.01, 0.02, 0.03], method="bonferroni")

    assert result["adjusted_p_values"] == [0.03, 0.06, 0.09]


def test_correct_p_values_fdr_bh_never_rejects_less_than_bonferroni() -> None:
    p_values = [0.001, 0.01, 0.02, 0.5]

    bonf = StatisticalToolkit.correct_p_values(p_values, method="bonferroni")
    bh = StatisticalToolkit.correct_p_values(p_values, method="fdr_bh")

    assert sum(bh["rejected"]) >= sum(bonf["rejected"])


def test_correct_p_values_on_an_empty_family() -> None:
    result = StatisticalToolkit.correct_p_values([])

    assert result == {"method": "fdr_bh", "adjusted_p_values": [], "rejected": []}


def test_evaluate_classifier_scores_a_perfect_prediction() -> None:
    result = StatisticalToolkit.evaluate_classifier(["yes", "no", "yes", "no"], ["yes", "no", "yes", "no"])

    assert result["accuracy"] == 1.0
    assert result["f1"] == 1.0


def test_evaluate_classifier_requires_a_binary_outcome() -> None:
    result = StatisticalToolkit.evaluate_classifier(["a", "b", "c"], ["a", "b", "c"])

    assert "error" in result

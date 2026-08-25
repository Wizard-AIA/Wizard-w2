"""Method registry: applicability gating and refusal (Phase 5).

Each "runs" test picks a method whose assumptions the fixture genuinely satisfies; each
"refused" test picks a fixture that clearly violates one named assumption and checks the
refusal names it and, where one exists, offers the right alternative.
"""

from __future__ import annotations

import pandas as pd

from src.core.analysis.methods import REGISTRY, run_method


def test_registry_lists_every_documented_method() -> None:
    assert {
        "independent_t_test",
        "one_way_anova",
        "paired_comparison",
        "chi_square_test",
        "pearson_correlation",
        "spearman_correlation",
        "linear_regression",
        "logistic_regression",
    } <= set(REGISTRY)


def test_unknown_method_is_reported_rather_than_raising() -> None:
    result = run_method("astrology", pd.DataFrame({"a": [1]}))

    assert result["status"] == "unknown_method"
    assert "astrology" not in result["known_methods"]


def test_independent_t_test_runs_when_exactly_two_groups() -> None:
    df = pd.DataFrame({"value": [1, 2, 3, 10, 11, 12], "group": list("aaabbb")})

    result = run_method("independent_t_test", df, value_col="value", group_col="group")

    assert result["status"] == "ok"
    assert result["result"]["significant"] is True


def test_independent_t_test_is_refused_for_three_groups_and_anova_is_offered() -> None:
    df = pd.DataFrame({"value": [1, 2, 3, 4, 5, 6], "group": list("aabbcc")})

    result = run_method("independent_t_test", df, value_col="value", group_col="group")

    assert result["status"] == "refused"
    assert any("3 groups" in reason for reason in result["reasons"])
    assert result["alternative"] == "one_way_anova"


def test_independent_t_test_is_refused_for_a_non_numeric_value_column() -> None:
    df = pd.DataFrame({"value": ["x", "y"], "group": ["a", "b"]})

    result = run_method("independent_t_test", df, value_col="value", group_col="group")

    assert result["status"] == "refused"
    assert any("not numeric" in reason for reason in result["reasons"])


def test_pearson_correlation_is_refused_for_non_normal_data_and_spearman_is_offered() -> None:
    df = pd.DataFrame({"x": list(range(30)), "y": [v**5 for v in range(30)]})

    result = run_method("pearson_correlation", df, col_a="x", col_b="y")

    assert result["status"] == "refused"
    assert result["alternative"] == "spearman_correlation"


def test_spearman_correlation_runs_on_the_same_non_normal_data() -> None:
    df = pd.DataFrame({"x": list(range(30)), "y": [v**5 for v in range(30)]})

    result = run_method("spearman_correlation", df, col_a="x", col_b="y")

    assert result["status"] == "ok"
    assert result["result"]["r"] > 0.99


def test_chi_square_test_is_refused_when_a_column_has_only_one_category() -> None:
    df = pd.DataFrame({"a": ["x"] * 10, "b": list("0101010101")})

    result = run_method("chi_square_test", df, col_a="a", col_b="b")

    assert result["status"] == "refused"


def test_linear_regression_is_refused_with_too_few_rows() -> None:
    df = pd.DataFrame({"y": [1, 2], "x": [1, 2]})

    result = run_method("linear_regression", df, target="y", features=["x"])

    assert result["status"] == "refused"
    assert any("complete rows" in reason for reason in result["reasons"])


def test_logistic_regression_is_refused_for_a_non_binary_target() -> None:
    df = pd.DataFrame({"y": ["a", "b", "c", "a"], "x": [1, 2, 3, 4]})

    result = run_method("logistic_regression", df, target="y", features=["x"])

    assert result["status"] == "refused"
    assert any("3 classes" in reason for reason in result["reasons"])


def test_logistic_regression_runs_for_a_binary_target() -> None:
    df = pd.DataFrame({"y": ["a", "a", "b", "b"] * 5, "x": [1, 2, 10, 11] * 5})

    result = run_method("logistic_regression", df, target="y", features=["x"])

    assert result["status"] == "ok"

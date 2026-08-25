"""Adversarial critic: deterministic detectors over the Section 10 catalogue (Phase 7).

Each detector is tested standalone against a fixture engineered to make the flaw unambiguous, the
same pattern `test_analysis_understanding.py` uses for its detectors -- this module's are no less
concrete just because they judge an already-produced result rather than the raw data.
"""

from __future__ import annotations

import pandas as pd

from src.core.analysis.critic import (
    CriticFinding,
    candidate_grouping_columns,
    critique,
    detect_aggregation_artifact,
    detect_join_problems,
    detect_leakage,
    detect_misleading_visualisation,
    detect_multiple_comparisons,
    detect_overclaiming,
    detect_overfitting,
    detect_simpsons_paradox,
    detect_wrong_test,
    find_simpsons_paradoxes,
)
from src.core.analysis.validation.base import ValidationContext


# --------------------------------------------------------------------------- #
# Simpson's paradox -- Phase 7's acceptance fixture
# --------------------------------------------------------------------------- #
def _kidney_stone_fixture() -> pd.DataFrame:
    """The textbook Simpson's-paradox dataset (Charig et al. 1986): treatment A has the higher
    success rate for both small and large stones, but treatment B has the higher rate overall,
    because B was used mostly on the easier small-stone cases."""
    counts = [
        ("A", "small", 1, 81),
        ("A", "small", 0, 6),
        ("A", "large", 1, 192),
        ("A", "large", 0, 71),
        ("B", "small", 1, 234),
        ("B", "small", 0, 36),
        ("B", "large", 1, 55),
        ("B", "large", 0, 25),
    ]
    rows = [
        {"treatment": treatment, "stone_size": size, "success": outcome}
        for treatment, size, outcome, n in counts
        for _ in range(n)
    ]
    return pd.DataFrame(rows)


def test_candidate_grouping_columns_finds_low_cardinality_columns() -> None:
    df = _kidney_stone_fixture()

    assert set(candidate_grouping_columns(df)) >= {"treatment", "stone_size"}


def test_detect_simpsons_paradox_flags_a_reversal_that_holds_in_every_segment() -> None:
    df = _kidney_stone_fixture()

    finding = detect_simpsons_paradox(df, outcome="success", group="treatment", segment="stone_size")

    assert finding is not None
    assert finding.category == "simpsons_paradox"
    assert finding.severity == "error"
    assert finding.suggested_reaction == "revise"


def test_detect_simpsons_paradox_is_none_when_a_segment_agrees_with_the_aggregate() -> None:
    """Not a paradox: the reversal must hold in every segment, not just some."""
    df = pd.DataFrame(
        {
            "dept": ["A"] * 4 + ["B"] * 4,
            "difficulty": ["easy", "easy", "hard", "hard"] * 2,
            "admitted": [1, 1, 1, 1, 0, 0, 0, 0],  # A wins overall AND in every segment
        }
    )

    assert detect_simpsons_paradox(df, "admitted", "dept", "difficulty") is None


def test_detect_simpsons_paradox_is_none_for_unknown_columns() -> None:
    df = pd.DataFrame({"a": [1, 2], "b": [1, 2]})

    assert detect_simpsons_paradox(df, "missing", "a", "b") is None


def test_find_simpsons_paradoxes_locates_the_pair_by_searching_candidates() -> None:
    df = _kidney_stone_fixture()

    findings = find_simpsons_paradoxes(df, outcome="success")

    assert any(f.category == "simpsons_paradox" for f in findings)


def test_critique_surfaces_the_paradox_from_code_that_only_names_the_outcome_column() -> None:
    """The agent's acceptance path: `critique()` finds it from context alone, no explicit
    group/segment columns supplied -- the same structural search `find_simpsons_paradoxes` does."""
    df = _kidney_stone_fixture()
    ctx = ValidationContext(code="df['success'].mean()", df=df)

    findings = critique(ctx)

    assert any(f.category == "simpsons_paradox" for f in findings)


# --------------------------------------------------------------------------- #
# Wrong test
# --------------------------------------------------------------------------- #
def test_detect_wrong_test_reports_a_refused_method() -> None:
    df = pd.DataFrame({"value": [1, 2, 3, 4, 5, 6], "group": list("aabbcc")})

    finding = detect_wrong_test("independent_t_test", df, value_col="value", group_col="group")

    assert finding is not None
    assert finding.category == "wrong_test"
    assert "one_way_anova" in finding.detail


def test_detect_wrong_test_is_none_when_the_method_fits() -> None:
    df = pd.DataFrame({"value": [1, 2, 3, 10, 11, 12], "group": list("aaabbb")})

    assert detect_wrong_test("independent_t_test", df, value_col="value", group_col="group") is None


# --------------------------------------------------------------------------- #
# Multiple comparisons / overfitting
# --------------------------------------------------------------------------- #
def test_detect_multiple_comparisons_flags_uncorrected_repeated_testing() -> None:
    code = "stats.ttest_ind(a, b)\nstats.pearsonr(a, c)\nstats.pearsonr(a, d)"

    findings = detect_multiple_comparisons(code)

    assert findings and findings[0].category == "multiple_comparisons"


def test_detect_multiple_comparisons_is_silent_once_corrected() -> None:
    code = "stats.ttest_ind(a, b)\nstats.pearsonr(a, c)\ncorrect_p_values([...], method='fdr_bh')"

    assert detect_multiple_comparisons(code) == []


def test_detect_multiple_comparisons_is_silent_for_a_single_test() -> None:
    assert detect_multiple_comparisons("stats.ttest_ind(a, b)") == []


def test_detect_overfitting_flags_a_scored_model_with_no_holdout() -> None:
    code = "model.fit(X, y)\nprint(model.score(X, y))"

    findings = detect_overfitting(code)

    assert findings and findings[0].category == "overfitting"


def test_detect_overfitting_is_silent_with_a_train_test_split() -> None:
    code = "X_train, X_test, y_train, y_test = train_test_split(X, y)\nmodel.fit(X_train, y_train)\nmodel.score(X_test, y_test)"

    assert detect_overfitting(code) == []


def test_detect_overfitting_is_silent_when_nothing_was_scored() -> None:
    assert detect_overfitting("model.fit(X, y)") == []


# --------------------------------------------------------------------------- #
# Reused signals
# --------------------------------------------------------------------------- #
def test_detect_leakage_wraps_the_understanding_profile() -> None:
    ctx = ValidationContext(understanding={"leakage": [{"feature": "leaky", "correlation": 1.0}]})

    findings = detect_leakage(ctx)

    assert findings[0].category == "leakage"
    assert findings[0].severity == "error"
    assert findings[0].suggested_reaction == "re_analyse"


def test_detect_join_problems_flags_a_dirty_key() -> None:
    ctx = ValidationContext(
        understanding={
            "join_keys": [
                {"left_table": "o", "right_table": "c", "left_column": "id", "right_column": "id", "dirty": True}
            ]
        }
    )

    findings = detect_join_problems(ctx)

    assert findings[0].category == "join"


def test_detect_aggregation_artifact_flags_mixed_grain_without_grouping() -> None:
    ctx = ValidationContext(code="df['amount'].sum()", understanding={"grain": {"mixed": True, "columns": ["id"]}})

    findings = detect_aggregation_artifact(ctx)

    assert findings[0].category == "aggregation_artifact"


def test_detect_misleading_visualisation_flags_an_untitled_chart() -> None:
    findings = detect_misleading_visualisation(ValidationContext(code="plt.plot(df['x'])"))

    assert findings[0].category == "misleading_visualisation"


def test_detect_overclaiming_splits_causal_language_from_plain_overclaiming() -> None:
    causal_ctx = ValidationContext(plan="check correlation", code="df.corr()", output="This proves a causal effect.")
    plain_ctx = ValidationContext(plan="run a t-test", code="stats.ttest_ind(a, b)", output="Significant.")

    causal = detect_overclaiming(causal_ctx)
    plain = detect_overclaiming(plain_ctx)

    assert any(f.category == "unsupported_causal_language" for f in causal)
    assert any(f.category == "overclaiming" for f in plain)


# --------------------------------------------------------------------------- #
# Finding
# --------------------------------------------------------------------------- #
def test_finding_round_trips_to_a_dict() -> None:
    finding = CriticFinding("leakage", "error", "msg", "detail", "re_analyse")

    assert finding.to_dict() == {
        "category": "leakage",
        "severity": "error",
        "message": "msg",
        "detail": "detail",
        "suggested_reaction": "re_analyse",
    }

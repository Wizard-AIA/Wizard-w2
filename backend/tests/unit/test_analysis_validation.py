"""Pluggable validation framework: each validator standalone, then the registry's tier gating.

Phase 6's acceptance criterion is validator selection, not new detection logic -- most validators
here re-surface evidence Phases 3-5 already computed (understanding profile, methods registry,
outlier detection), so each test checks that the right validator fires on the right evidence, not
that the underlying detector works (that is already covered by its own module's tests).
"""

from __future__ import annotations

import pandas as pd

from src.core.analysis.validation.alternative import AlternativeValidator
from src.core.analysis.validation.base import Finding, ValidationContext
from src.core.analysis.validation.computational import ComputationalValidator
from src.core.analysis.validation.consistency import ConsistencyValidator
from src.core.analysis.validation.data import DataValidator
from src.core.analysis.validation.registry import ALL_VALIDATORS, TIER_VALIDATORS, cache_key, run_validators
from src.core.analysis.validation.reproducibility import ReproducibilityValidator
from src.core.analysis.validation.semantic import SemanticValidator
from src.core.analysis.validation.sensitivity import SensitivityValidator
from src.core.analysis.validation.statistical import StatisticalValidator


# --------------------------------------------------------------------------- #
# Finding
# --------------------------------------------------------------------------- #
def test_finding_round_trips_to_a_dict() -> None:
    finding = Finding(validator="data", severity="warning", message="msg", detail="detail")

    assert finding.to_dict() == {"validator": "data", "severity": "warning", "message": "msg", "detail": "detail"}


# --------------------------------------------------------------------------- #
# Computational
# --------------------------------------------------------------------------- #
def test_computational_validator_is_not_applicable_without_a_recomputation() -> None:
    assert ComputationalValidator().applicable(ValidationContext()) is False


def test_computational_validator_reports_a_mismatch_as_an_error() -> None:
    ctx = ValidationContext(recomputation_status="mismatch", recomputation_detail="got 15 expected 99")

    findings = ComputationalValidator().validate(ctx)

    assert findings[0].severity == "error"
    assert "disagreed" in findings[0].message


def test_computational_validator_reports_a_match_as_info() -> None:
    ctx = ValidationContext(recomputation_status="verified", recomputation_detail="VERIFIED: 15")

    findings = ComputationalValidator().validate(ctx)

    assert findings[0].severity == "info"


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
def test_data_validator_surfaces_a_dirty_join_key() -> None:
    ctx = ValidationContext(
        understanding={
            "grain": {"mixed": False, "columns": []},
            "join_keys": [
                {
                    "left_table": "orders",
                    "right_table": "customers",
                    "left_column": "id",
                    "right_column": "id",
                    "dirty": True,
                }
            ],
            "leakage": [],
            "type_anomalies": [],
        }
    )

    findings = DataValidator().validate(ctx)

    assert any("Dirty join key" in f.message for f in findings)


def test_data_validator_is_clean_for_a_clean_profile() -> None:
    ctx = ValidationContext(
        understanding={"grain": {"mixed": False}, "join_keys": [], "leakage": [], "type_anomalies": []}
    )

    assert DataValidator().validate(ctx) == []


def test_data_validator_is_not_applicable_without_an_understanding_profile() -> None:
    assert DataValidator().applicable(ValidationContext()) is False


# --------------------------------------------------------------------------- #
# Semantic
# --------------------------------------------------------------------------- #
def test_semantic_validator_flags_an_untitled_matplotlib_chart() -> None:
    ctx = ValidationContext(code="plt.plot(df['x'])\nplt.savefig('out.png')")

    findings = SemanticValidator().validate(ctx)

    assert any("no title" in f.message for f in findings)


def test_semantic_validator_is_not_applicable_to_non_plotting_code() -> None:
    assert SemanticValidator().applicable(ValidationContext(code="print(df.sum())")) is False


def test_semantic_validator_is_clean_for_a_labelled_chart() -> None:
    code = "plt.plot(df['x'])\nplt.title('t')\nplt.xlabel('x')\nplt.ylabel('y')"

    assert SemanticValidator().validate(ValidationContext(code=code)) == []


# --------------------------------------------------------------------------- #
# Statistical
# --------------------------------------------------------------------------- #
def test_statistical_validator_flags_significance_without_a_p_value() -> None:
    ctx = ValidationContext(plan="run a t-test", code="stats.ttest_ind(a, b)", output="The difference is significant.")

    findings = StatisticalValidator().validate(ctx)

    assert any("no p-value" in f.message for f in findings)


def test_statistical_validator_is_not_applicable_to_unrelated_work() -> None:
    ctx = ValidationContext(plan="count rows", code="print(len(df))", output="10")

    assert StatisticalValidator().applicable(ctx) is False


def test_statistical_validator_is_clean_when_a_p_value_is_reported() -> None:
    ctx = ValidationContext(plan="run a t-test", code="stats.ttest_ind(a, b)", output="Significant, p-value=0.01.")

    assert StatisticalValidator().validate(ctx) == []


# --------------------------------------------------------------------------- #
# Sensitivity
# --------------------------------------------------------------------------- #
def test_sensitivity_validator_flags_an_aggregate_over_an_outlier_heavy_column() -> None:
    df = pd.DataFrame({"amount": [10, 11, 9, 10, 12, 1000]})
    ctx = ValidationContext(code="df['amount'].mean()", df=df)

    findings = SensitivityValidator().validate(ctx)

    assert any("outliers" in f.message for f in findings)


def test_sensitivity_validator_ignores_a_column_the_code_never_touches() -> None:
    df = pd.DataFrame({"amount": [10, 11, 9, 10, 12, 1000], "other": [1, 2, 3, 4, 5, 6]})
    ctx = ValidationContext(code="df['other'].mean()", df=df)

    assert SensitivityValidator().validate(ctx) == []


def test_sensitivity_validator_is_not_applicable_without_an_aggregate() -> None:
    df = pd.DataFrame({"amount": [1, 2, 3]})
    assert SensitivityValidator().applicable(ValidationContext(code="df.head()", df=df)) is False


# --------------------------------------------------------------------------- #
# Alternative
# --------------------------------------------------------------------------- #
def test_alternative_validator_names_the_registered_alternative() -> None:
    ctx = ValidationContext(method="independent_t_test")

    findings = AlternativeValidator().validate(ctx)

    assert findings[0].detail == "one_way_anova"


def test_alternative_validator_is_not_applicable_without_a_named_method() -> None:
    assert AlternativeValidator().applicable(ValidationContext()) is False


def test_alternative_validator_is_silent_when_the_method_has_no_alternative() -> None:
    ctx = ValidationContext(method="one_way_anova")

    assert AlternativeValidator().validate(ctx) == []


# --------------------------------------------------------------------------- #
# Consistency
# --------------------------------------------------------------------------- #
def test_consistency_validator_flags_an_ungrouped_aggregate_over_mixed_grain() -> None:
    ctx = ValidationContext(
        code="df['amount'].sum()", understanding={"grain": {"mixed": True, "columns": ["order_id"]}}
    )

    findings = ConsistencyValidator().validate(ctx)

    assert any("does not appear to group" in f.message for f in findings)


def test_consistency_validator_is_clean_when_the_code_groups_first() -> None:
    ctx = ValidationContext(
        code="df.groupby('order_id')['amount'].first().sum()",
        understanding={"grain": {"mixed": True, "columns": ["order_id"]}},
    )

    assert ConsistencyValidator().validate(ctx) == []


def test_consistency_validator_is_not_applicable_to_clean_grain() -> None:
    ctx = ValidationContext(understanding={"grain": {"mixed": False}})

    assert ConsistencyValidator().applicable(ctx) is False


# --------------------------------------------------------------------------- #
# Reproducibility
# --------------------------------------------------------------------------- #
def test_reproducibility_validator_flags_unseeded_sampling() -> None:
    findings = ReproducibilityValidator().validate(ValidationContext(code="df.sample(10)"))

    assert findings and "not be exactly reproducible" in findings[0].message


def test_reproducibility_validator_is_clean_when_seeded() -> None:
    ctx = ValidationContext(code="df.sample(10, random_state=42)")

    assert ReproducibilityValidator().validate(ctx) == []


def test_reproducibility_validator_is_not_applicable_without_randomness() -> None:
    assert ReproducibilityValidator().applicable(ValidationContext(code="df.sum()")) is False


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def test_registry_lists_all_eight_validators() -> None:
    assert set(ALL_VALIDATORS) == {
        "computational",
        "data",
        "semantic",
        "statistical",
        "sensitivity",
        "alternative",
        "consistency",
        "reproducibility",
    }


def test_balanced_tier_never_runs_every_validator() -> None:
    assert set(TIER_VALIDATORS["balanced"]) < set(ALL_VALIDATORS)


def test_full_tier_runs_every_validator() -> None:
    assert set(TIER_VALIDATORS["full"]) == set(ALL_VALIDATORS)


def test_run_validators_only_returns_findings_from_applicable_validators() -> None:
    ctx = ValidationContext(
        code="df.sample(5)",
        recomputation_status="mismatch",
        recomputation_detail="got 1 expected 2",
    )

    findings = run_validators(ctx, tier="balanced")

    validators_that_fired = {f.validator for f in findings}
    assert "computational" in validators_that_fired
    assert "reproducibility" in validators_that_fired  # unseeded .sample(
    assert "sensitivity" not in validators_that_fired  # balanced tier excludes it
    assert "semantic" not in validators_that_fired  # no plotting code


# --------------------------------------------------------------------------- #
# cache_key (Phase 14) -- every validator is a pure function of ValidationContext, so an
# identical key must mean an identical result is guaranteed, never approximated.
# --------------------------------------------------------------------------- #
def test_cache_key_is_identical_for_identical_inputs() -> None:
    kwargs = {
        "tier": "balanced",
        "code": "df.sum()",
        "content_hash": "abc",
        "method": "",
        "recomputation_status": "verified",
    }

    assert cache_key(**kwargs) == cache_key(**kwargs)


def test_cache_key_changes_when_the_dataset_changes() -> None:
    kwargs = {"tier": "balanced", "code": "df.sum()", "method": "", "recomputation_status": "verified"}

    assert cache_key(**kwargs, content_hash="abc") != cache_key(**kwargs, content_hash="xyz")


def test_cache_key_changes_when_the_code_changes() -> None:
    kwargs = {"tier": "balanced", "content_hash": "abc", "method": "", "recomputation_status": "verified"}

    assert cache_key(**kwargs, code="df.sum()") != cache_key(**kwargs, code="df.mean()")


def test_cache_key_changes_when_the_verification_outcome_changes() -> None:
    kwargs = {"tier": "balanced", "code": "df.sum()", "content_hash": "abc", "method": ""}

    assert cache_key(**kwargs, recomputation_status="verified") != cache_key(**kwargs, recomputation_status="mismatch")


def test_run_validators_on_full_tier_reaches_sensitivity() -> None:
    df = pd.DataFrame({"amount": [10, 11, 9, 10, 12, 1000]})
    ctx = ValidationContext(code="df['amount'].mean()", df=df)

    findings = run_validators(ctx, tier="full")

    assert any(f.validator == "sensitivity" for f in findings)


def test_run_validators_on_an_unknown_tier_falls_back_to_balanced() -> None:
    ctx = ValidationContext(recomputation_status="verified", recomputation_detail="ok")

    assert run_validators(ctx, tier="compact") == run_validators(ctx, tier="balanced")

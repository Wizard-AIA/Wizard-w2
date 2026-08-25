"""Adaptive data understanding: grain, join-key consistency, temporal coverage, leakage.

Each detector is tested standalone against a small deliberately-flawed fixture, then
`understand()`'s own gating (objective-driven, not wholesale) is tested separately.
"""

from __future__ import annotations

import pandas as pd

from src.core.analysis.understanding import (
    candidate_identifiers,
    candidate_join_keys,
    cardinality_profile,
    check_join_keys,
    infer_grain,
    leakage_indicators,
    referential_consistency,
    render,
    resolve_time_column,
    temporal_coverage,
    type_anomalies,
    understand,
)


# --------------------------------------------------------------------------- #
# Grain
# --------------------------------------------------------------------------- #
def test_candidate_identifiers_finds_a_fully_unique_column() -> None:
    df = pd.DataFrame({"id": [1, 2, 3], "value": [10, 10, 20]})

    assert candidate_identifiers(df) == ["id"]


def test_candidate_identifiers_excludes_a_repeating_column() -> None:
    df = pd.DataFrame({"a": [1, 1, 2], "b": [4, 5, 6]})

    assert candidate_identifiers(df) == ["b"]


def test_infer_grain_recognises_a_clean_single_column_grain() -> None:
    df = pd.DataFrame({"id": [1, 2, 3], "value": [10, 10, 20]})

    grain = infer_grain(df)

    assert grain == {"grain": "single_column", "columns": ["id"], "mixed": False}


def test_infer_grain_flags_a_hinted_identifier_that_repeats() -> None:
    """order_id claims a per-order record, but the same id recurs -- the table is really at a
    finer grain than its own naming suggests."""
    df = pd.DataFrame({"order_id": [1, 1, 2, 3], "item": ["a", "b", "a", "c"]})

    grain = infer_grain(df)

    assert grain["mixed"] is True
    assert grain["grain"] == "mixed"
    assert "order_id" in grain["columns"]


def test_infer_grain_falls_back_to_the_whole_row_when_nothing_is_hinted() -> None:
    df = pd.DataFrame({"a": [1, 1, 2], "b": [4, 5, 4]})

    grain = infer_grain(df)

    assert grain == {"grain": "row", "columns": [], "mixed": False}


# --------------------------------------------------------------------------- #
# Cardinality / type anomalies
# --------------------------------------------------------------------------- #
def test_cardinality_profile_reports_unique_ratio() -> None:
    df = pd.DataFrame({"id": [1, 2, 3, 4]})

    profile = cardinality_profile(df)

    assert profile["id"]["unique_values"] == 4
    assert profile["id"]["unique_ratio"] == 1.0


def test_type_anomalies_flags_a_column_mixing_numeric_and_non_numeric_strings() -> None:
    df = pd.DataFrame(
        {
            "mixed_col": ["1", "2", "abc", "3"],
            "clean_numeric_strings": ["1", "2", "3", "4"],
            "clean_text": ["a", "b", "c", "d"],
        }
    )

    anomalies = type_anomalies(df)

    assert [entry["column"] for entry in anomalies] == ["mixed_col"]
    assert anomalies[0]["numeric_like_ratio"] == 0.75


def test_type_anomalies_is_empty_for_a_clean_frame() -> None:
    df = pd.DataFrame({"numeric": [1, 2, 3], "text": ["a", "b", "c"]})

    assert type_anomalies(df) == []


# --------------------------------------------------------------------------- #
# Join keys
# --------------------------------------------------------------------------- #
def test_candidate_join_keys_finds_shared_column_names() -> None:
    left = pd.DataFrame({"customer_id": [1], "amount": [1]})
    right = pd.DataFrame({"customer_id": [1], "name": ["a"]})

    pairs = candidate_join_keys({"orders": left, "customers": right})

    assert pairs == [
        {
            "left_table": "orders",
            "right_table": "customers",
            "left_column": "customer_id",
            "right_column": "customer_id",
        }
    ]


def test_referential_consistency_flags_a_dirty_join_key() -> None:
    """Acceptance fixture: an int id on one side, a zero-padded string on the other -- the same
    entities, formatted differently, so the raw match rate is near zero but the normalised one
    is not."""
    orders = pd.DataFrame({"customer_id": [1, 2, 3, 4, 5]})
    customers = pd.DataFrame({"customer_id": ["01", "02", "03", "04", "05"]})

    result = referential_consistency(orders, "customer_id", customers, "customer_id")

    assert result["raw_match_rate"] == 0.0
    assert result["normalised_match_rate"] == 1.0
    assert result["dtype_mismatch"] is True
    assert result["dirty"] is True


def test_referential_consistency_is_clean_for_a_well_formed_join() -> None:
    orders = pd.DataFrame({"customer_id": [1, 2, 3]})
    customers = pd.DataFrame({"customer_id": [1, 2, 3]})

    result = referential_consistency(orders, "customer_id", customers, "customer_id")

    assert result["raw_match_rate"] == 1.0
    assert result["dirty"] is False


def test_check_join_keys_runs_referential_consistency_over_every_candidate_pair() -> None:
    orders = pd.DataFrame({"customer_id": [1, 2, 3]})
    customers = pd.DataFrame({"customer_id": ["01", "02", "03"]})

    findings = check_join_keys({"orders": orders, "customers": customers})

    assert len(findings) == 1
    assert findings[0]["dirty"] is True
    assert findings[0]["left_table"] == "orders"


# --------------------------------------------------------------------------- #
# Temporal coverage
# --------------------------------------------------------------------------- #
def test_temporal_coverage_reports_the_date_range() -> None:
    df = pd.DataFrame({"created_at": ["2024-01-01", "2024-01-05", "2024-01-10"]})

    coverage = temporal_coverage(df, "created_at")

    assert coverage["rows"] == 3
    assert coverage["span_days"] == 9


def test_temporal_coverage_on_an_unparseable_column_reports_zero_rows() -> None:
    df = pd.DataFrame({"created_at": ["not", "a", "date"]})

    assert temporal_coverage(df, "created_at") == {"column": "created_at", "rows": 0}


# --------------------------------------------------------------------------- #
# Leakage
# --------------------------------------------------------------------------- #
def test_leakage_indicators_flags_a_near_perfect_correlate() -> None:
    """Acceptance fixture: `leaky` is target*2 -- a restatement, not a real predictor."""
    df = pd.DataFrame({"target": [1, 2, 3, 4, 5], "leaky": [2, 4, 6, 8, 10], "unrelated": [5, 3, 1, 4, 2]})

    indicators = leakage_indicators(df, "target")

    assert [entry["feature"] for entry in indicators] == ["leaky"]


def test_leakage_indicators_is_empty_when_nothing_correlates_that_strongly() -> None:
    df = pd.DataFrame({"target": [1, 2, 3, 4, 5], "unrelated": [5, 3, 1, 4, 2]})

    assert leakage_indicators(df, "target") == []


def test_leakage_indicators_on_an_unknown_target_is_empty() -> None:
    df = pd.DataFrame({"a": [1, 2, 3]})

    assert leakage_indicators(df, "missing") == []


# --------------------------------------------------------------------------- #
# resolve_time_column
# --------------------------------------------------------------------------- #
def test_resolve_time_column_picks_the_one_column_profiled_as_temporal() -> None:
    catalog = {"columns": {"created_at": {"semantic_type": "temporal"}, "amount": {"semantic_type": "numeric"}}}

    assert resolve_time_column(catalog) == "created_at"


def test_resolve_time_column_is_none_when_there_are_several_candidates() -> None:
    """Two temporal columns: picking one over the other would be a guess, not a fact."""
    catalog = {"columns": {"created_at": {"semantic_type": "temporal"}, "updated_at": {"semantic_type": "temporal"}}}

    assert resolve_time_column(catalog) is None


def test_resolve_time_column_is_none_with_no_temporal_column_or_no_catalog() -> None:
    assert resolve_time_column({"columns": {"amount": {"semantic_type": "numeric"}}}) is None
    assert resolve_time_column(None) is None


# --------------------------------------------------------------------------- #
# understand() -- objective-driven gating
# --------------------------------------------------------------------------- #
def test_understand_always_computes_the_cheap_structural_facts() -> None:
    df = pd.DataFrame({"id": [1, 2, 3], "value": [1, 2, 3]})

    profile = understand(df)

    assert profile["grain"]["grain"] == "single_column"
    assert "id" in profile["cardinality"]
    assert profile["join_keys"] == []
    assert profile["leakage"] == []
    assert profile["temporal"] is None


def test_understand_checks_join_keys_whenever_more_than_one_table_is_loaded() -> None:
    orders = pd.DataFrame({"customer_id": [1, 2, 3]})
    customers = pd.DataFrame({"customer_id": ["01", "02", "03"]})

    profile = understand(orders, tables={"orders": orders, "customers": customers})

    assert profile["join_keys"] and profile["join_keys"][0]["dirty"] is True


def test_understand_runs_leakage_only_for_a_relevant_analytical_type() -> None:
    df = pd.DataFrame({"target": [1, 2, 3, 4, 5], "leaky": [2, 4, 6, 8, 10]})

    relevant = understand(df, target="target", analytical_type="predictive")
    irrelevant = understand(df, target="target", analytical_type="descriptive")
    unstated = understand(df, target="target")

    assert relevant["leakage"]
    assert irrelevant["leakage"] == []
    assert unstated["leakage"]


def test_understand_runs_temporal_only_for_a_relevant_analytical_type() -> None:
    df = pd.DataFrame({"created_at": ["2024-01-01", "2024-01-05"]})

    relevant = understand(df, time_column="created_at", analytical_type="forecasting")
    irrelevant = understand(df, time_column="created_at", analytical_type="descriptive")

    assert relevant["temporal"] is not None
    assert irrelevant["temporal"] is None


def test_understand_never_guesses_a_target_or_time_column() -> None:
    """No target/time_column supplied -- no correlation scan, no date parsing attempted."""
    df = pd.DataFrame({"target": [1, 2, 3], "created_at": ["2024-01-01", "2024-01-02", "2024-01-03"]})

    profile = understand(df)

    assert profile["leakage"] == []
    assert profile["temporal"] is None


# --------------------------------------------------------------------------- #
# render()
# --------------------------------------------------------------------------- #
def test_render_is_empty_for_a_clean_profile() -> None:
    df = pd.DataFrame({"id": [1, 2, 3], "value": [1, 2, 3]})

    assert render(understand(df)) == ""


def test_render_surfaces_every_kind_of_finding() -> None:
    profile = {
        "grain": {"grain": "mixed", "columns": ["order_id"], "mixed": True},
        "join_keys": [
            {
                "left_table": "orders",
                "right_table": "customers",
                "left_column": "customer_id",
                "right_column": "customer_id",
                "dirty": True,
                "dtype_mismatch": True,
                "raw_match_rate": 0.0,
                "normalised_match_rate": 1.0,
            }
        ],
        "leakage": [{"feature": "leaky", "correlation": 1.0, "reason": "near-perfect correlation with the target"}],
        "type_anomalies": [{"column": "mixed_col", "numeric_like_ratio": 0.75, "reason": "..."}],
    }

    notes = render(profile)

    assert "Mixed grain" in notes
    assert "Dirty join key" in notes
    assert "target leakage" in notes
    assert "mixed_col" in notes

"""Unit tests for profiling, statistics, scoring, retrieval and the schema registry."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.core.database import db_mgr
from src.core.llm.registry import classify
from src.core.prompts import create_answer_prompt, create_prompt, generate_system_context
from src.core.rag.retriever import ContextRetriever, lexical_overlap, tokenize
from src.core.tools.catalog import CatalogEngine
from src.core.tools.evaluator import Evaluator
from src.core.tools.schema_registry import SchemaRegistry
from src.core.tools.stats import StatisticalToolkit


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #
def test_catalog_reports_shape_and_completeness(simple_df: pd.DataFrame) -> None:
    catalog = CatalogEngine.analyze(simple_df)
    assert set(catalog["columns"]) == {"A", "B", "C"}
    assert catalog["global_quality"]["completeness_score"] == 100.0
    assert catalog["global_quality"]["rows"] == 5


def test_catalog_counts_missing_values(missing_values_df: pd.DataFrame) -> None:
    catalog = CatalogEngine.analyze(missing_values_df)
    assert catalog["global_quality"]["total_missing"] >= 2
    assert catalog["columns"]["A"]["quality"]["missing_percentage"] > 0


@pytest.mark.parametrize(
    "column,expected",
    [
        ("user_id", "identifier"),
        ("total_price", "financial"),
        ("created_date", "temporal"),
        ("city", "geospatial"),
        ("customer_name", "personal"),
    ],
)
def test_semantic_type_detection_from_column_name(column: str, expected: str) -> None:
    df = pd.DataFrame({column: ["a", "b", "c"]})
    catalog = CatalogEngine.analyze(df)
    assert catalog["columns"][column]["semantic_type"] == expected


def test_catalog_samples_large_frames() -> None:
    df = pd.DataFrame({"a": range(50_000)})
    catalog = CatalogEngine.analyze(df, sample_rows=1000)

    assert catalog["global_quality"]["sampled"] is True
    assert catalog["global_quality"]["sample_rows"] == 1000
    assert catalog["global_quality"]["rows"] == 50_000


def test_catalog_on_empty_frame(empty_df: pd.DataFrame) -> None:
    catalog = CatalogEngine.analyze(empty_df)
    assert catalog["columns"] == {}
    assert catalog["global_quality"]["completeness_score"] == 100.0


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #
def test_normality_requires_enough_samples() -> None:
    result = StatisticalToolkit.check_normality(pd.DataFrame({"x": [1.0, 2.0]}), "x")
    assert result["is_normal"] is False
    assert result["p_value"] is None


def test_normality_on_a_normal_sample() -> None:
    rng = np.random.default_rng(3)
    df = pd.DataFrame({"x": rng.normal(size=500)})
    result = StatisticalToolkit.check_normality(df, "x")
    assert result["test_used"] == "Shapiro-Wilk"
    assert result["p_value"] is not None


def test_outlier_detection_finds_an_extreme_value() -> None:
    df = pd.DataFrame({"x": [1, 2, 3, 4, 5, 1000]})
    result = StatisticalToolkit.detect_outliers(df, "x")
    assert result["outlier_count"] >= 1
    assert 1000 in result["sample_outliers"]


def test_outlier_detection_zscore_method() -> None:
    df = pd.DataFrame({"x": [1.0] * 40 + [500.0]})
    result = StatisticalToolkit.detect_outliers(df, "x", method="zscore")
    assert result["method"] == "zscore"


def test_correlation_analysis_ranks_the_strongest_feature() -> None:
    df = pd.DataFrame({"target": [1, 2, 3, 4, 5], "strong": [2, 4, 6, 8, 10], "weak": [5, 1, 4, 2, 3]})
    results = StatisticalToolkit.correlation_analysis(df, "target")
    assert results[0]["feature"] == "strong"
    assert results[0]["strength"] == "Strong"


def test_correlation_analysis_on_missing_target() -> None:
    assert StatisticalToolkit.correlation_analysis(pd.DataFrame({"a": [1, 2]}), "absent") == []


# --------------------------------------------------------------------------- #
# Evaluator
# --------------------------------------------------------------------------- #
def test_evaluator_penalises_real_execution_errors() -> None:
    good = Evaluator.score_execution("The mean is 4.2 and the distribution is normal.")
    bad = Evaluator.score_execution("Error executing code:\nTraceback (most recent call last)")
    assert bad["score"] < good["score"]
    assert bad["status"] == "FAIL"


def test_evaluator_does_not_penalise_the_word_error_in_prose() -> None:
    """Regression: matching the bare substring "Error" docked 50 points from any
    output that merely mentioned it, e.g. a column named `error_rate`."""
    scored = Evaluator.score_execution("The mean error_rate is 0.02 with low variance.")
    assert scored["score"] >= 90
    assert scored["status"] == "PASS"


def test_evaluator_skips_rigour_check_for_non_analytical_requests() -> None:
    scored = Evaluator.score_execution("A B C", instruction="show me the column names")
    assert scored["score"] == 100


def test_evaluate_code_quality_flags_prohibited_calls() -> None:
    report = Evaluator.evaluate_code_quality("exec('x=1')")
    assert report["is_clean"] is False
    assert report["quality_rating"] == "Low"


def test_evaluate_code_quality_accepts_clean_code() -> None:
    report = Evaluator.evaluate_code_quality("import pandas as pd\nprint(pd.__version__)")
    assert report["is_clean"] is True
    assert report["warnings"] == []


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #
def test_tokenize_drops_stopwords() -> None:
    tokens = tokenize("show me the average revenue by region")
    assert "revenue" in tokens
    assert "region" in tokens
    assert "the" not in tokens
    assert "show" not in tokens


def test_lexical_overlap_scores_related_text_higher() -> None:
    query = tokenize("average revenue by region")
    assert lexical_overlap(query, "revenue") > lexical_overlap(query, "unrelated_field")


def test_column_selection_passes_through_narrow_frames(simple_df: pd.DataFrame) -> None:
    columns, truncated = ContextRetriever().select_columns("anything", simple_df)
    assert columns == ["A", "B", "C"]
    assert truncated is False


def test_column_selection_budgets_wide_frames(wide_df: pd.DataFrame) -> None:
    columns, truncated = ContextRetriever().select_columns("feature_7 trend", wide_df, max_columns=20)
    assert truncated is True
    assert len(columns) <= 20
    # A column named in the question is never dropped.
    assert "feature_7" in columns


def test_column_selection_preserves_frame_order(wide_df: pd.DataFrame) -> None:
    columns, _ = ContextRetriever().select_columns("feature_3", wide_df, max_columns=15)
    original = [c for c in wide_df.columns if c in set(columns)]
    assert columns == original


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #
def test_system_context_includes_shape_and_columns(simple_df: pd.DataFrame) -> None:
    context = generate_system_context(simple_df, query="mean of A")
    assert "5 rows" in context or "5," in context
    assert "A" in context


def test_system_context_is_bounded_for_wide_frames(wide_df: pd.DataFrame) -> None:
    """Regression: the previous builder dumped every column's describe() and
    unique values, which alone could exceed the model's context window."""
    context = generate_system_context(wide_df, query="feature_1 distribution", max_columns=20)
    assert "Showing 20 of 120 columns" in context
    assert len(context) < 20_000


def test_system_context_renders_notable_understanding_findings(simple_df: pd.DataFrame) -> None:
    profile = {
        "grain": {"grain": "mixed", "columns": ["order_id"], "mixed": True},
        "join_keys": [],
        "leakage": [],
        "type_anomalies": [],
    }

    context = generate_system_context(simple_df, query="mean of A", understanding=profile)

    assert "<data_understanding>" in context
    assert "Mixed grain" in context


def test_system_context_omits_the_understanding_block_when_nothing_is_notable(simple_df: pd.DataFrame) -> None:
    clean_profile = {
        "grain": {"grain": "row", "columns": [], "mixed": False},
        "join_keys": [],
        "leakage": [],
        "type_anomalies": [],
    }

    context = generate_system_context(simple_df, query="mean of A", understanding=clean_profile)

    assert "<data_understanding>" not in context


def test_worker_prompt_carries_plan_and_error(simple_df: pd.DataFrame) -> None:
    prompt = create_prompt(
        "plot A",
        simple_df,
        plan="1. Plot column A",
        previous_error="Error executing code:\nKeyError: 'Z'",
    )
    assert "<approved_plan>" in prompt
    assert "<previous_error>" in prompt
    assert "KeyError" in prompt


def test_worker_prompt_states_the_dataframe_is_preloaded(simple_df: pd.DataFrame) -> None:
    prompt = create_prompt("summarise", simple_df)
    assert "ALREADY loaded" in prompt
    assert "Never reload it from disk" in prompt


def test_answer_prompt_surfaces_a_critic_finding_and_the_reaction_instruction() -> None:
    prompt = create_answer_prompt(
        "how many rows",
        "print(len(df))",
        "10",
        critic_findings=["Possible target leakage via 'leaky'."],
    )

    assert "<critic_findings>" in prompt
    assert "Possible target leakage via 'leaky'." in prompt
    assert "weaken, qualify or flag as unresolved" in prompt


def test_answer_prompt_omits_the_critic_block_when_there_are_no_findings() -> None:
    prompt = create_answer_prompt("how many rows", "print(len(df))", "10")

    assert "<critic_findings>" not in prompt


# --------------------------------------------------------------------------- #
# Schema registry
# --------------------------------------------------------------------------- #
def test_registry_detects_a_conventional_primary_key() -> None:
    df = pd.DataFrame({"id": [1, 2, 3], "value": [10, 20, 30]})
    assert SchemaRegistry._detect_primary_key(df) == "id"


def test_registry_detects_a_suffixed_unique_key() -> None:
    df = pd.DataFrame({"order_id": [1, 2, 3], "amount": [5, 5, 5]})
    assert SchemaRegistry._detect_primary_key(df) == "order_id"


def test_registry_returns_empty_when_nothing_is_unique() -> None:
    df = pd.DataFrame({"a": [1, 1, 1], "b": [2, 2, 2]})
    assert SchemaRegistry._detect_primary_key(df) == ""


def test_registry_suggests_a_foreign_key_join() -> None:
    session_id = "join-test"
    SchemaRegistry.register_dataframe(
        "users.csv", pd.DataFrame({"id": [1, 2], "name": ["a", "b"]}), session_id=session_id
    )
    SchemaRegistry.register_dataframe(
        "orders.csv", pd.DataFrame({"order_id": [1, 2], "user_id": [1, 2]}), session_id=session_id
    )

    suggestions = SchemaRegistry.get_join_suggestions(session_id=session_id)
    pairs = {(match["col1"], match["col2"]) for entry in suggestions for match in entry["matching_columns"]}

    assert ("user_id", "id") in pairs or ("id", "user_id") in pairs
    db_mgr.delete_session_data(session_id)


def test_registry_scopes_schemas_to_a_session() -> None:
    SchemaRegistry.register_dataframe("a.csv", pd.DataFrame({"x": [1]}), session_id="s-one")
    SchemaRegistry.register_dataframe("b.csv", pd.DataFrame({"y": [1]}), session_id="s-two")

    names = {schema["filename"] for schema in db_mgr.get_schemas(session_id="s-one")}
    assert names == {"a.csv"}

    db_mgr.delete_session_data("s-one")
    db_mgr.delete_session_data("s-two")


# --------------------------------------------------------------------------- #
# Model registry classification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name,capability",
    [
        ("qwen2.5-coder:1.5b", "code"),
        ("deepseek-r1:7b", "reasoning"),
        ("llava:7b", "vision"),
        ("nomic-embed-text", "embedding"),
        ("llama3.2:3b", "general"),
    ],
)
def test_model_classification(name: str, capability: str) -> None:
    assert capability in classify(name)


def test_embedding_models_are_not_offered_for_chat() -> None:
    assert classify("all-minilm") == ["embedding"]

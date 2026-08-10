"""Unit + negative tests for multi-format ingestion."""

from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.core.ingest.loader import (
    DatasetLoader,
    EmptyDatasetError,
    UnsupportedFormatError,
    categorize_low_cardinality,
    downcast_numeric,
    json_safe_records,
    safe_write_feather,
    sanitize_columns,
)


# --------------------------------------------------------------------------- #
# Column sanitisation
# --------------------------------------------------------------------------- #
def test_sanitize_produces_code_safe_names() -> None:
    # Trailing separators are trimmed, so "tip %" collapses to "tip".
    columns, renamed = sanitize_columns(["Total Bill", "tip %", "day-of-week"])
    assert columns == ["Total_Bill", "tip", "day_of_week"]
    assert renamed["Total Bill"] == "Total_Bill"
    assert all(name.isidentifier() for name in columns)


def test_sanitize_deduplicates_collisions() -> None:
    """Regression: punctuation stripping used to map distinct names onto one.

    `a-b` and `a.b` both reduced to `ab`, producing a frame with duplicate
    column labels that later broke Feather writes and made selection ambiguous.
    """
    columns, _ = sanitize_columns(["a-b", "a.b", "a b"])
    assert len(set(columns)) == 3, f"expected unique names, got {columns}"


def test_sanitize_handles_many_collisions() -> None:
    columns, _ = sanitize_columns(["x!", "x@", "x#", "x$"])
    assert len(set(columns)) == 4


def test_sanitize_replaces_empty_and_numeric_leading_names() -> None:
    columns, _ = sanitize_columns(["", "  ", "2024", "!!!"])
    assert all(name.isidentifier() for name in columns)
    assert len(set(columns)) == 4


def test_sanitize_reports_only_changed_names() -> None:
    columns, renamed = sanitize_columns(["clean", "not clean"])
    assert columns[0] == "clean"
    assert "clean" not in renamed
    assert "not clean" in renamed


# --------------------------------------------------------------------------- #
# Format support
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["a.csv", "a.tsv", "a.xlsx", "a.json", "a.parquet", "a.feather", "A.CSV"])
def test_supported_extensions(name: str) -> None:
    assert DatasetLoader.is_supported(name)


@pytest.mark.parametrize("name", ["a.exe", "a.pdf", "a", "a.csv.exe", "a.docx"])
def test_unsupported_extensions(name: str) -> None:
    assert not DatasetLoader.is_supported(name)


def test_load_csv(tmp_path: Path, simple_df: pd.DataFrame) -> None:
    path = tmp_path / "data.csv"
    simple_df.to_csv(path, index=False)

    result = DatasetLoader.load(path)
    assert len(result.df) == 5
    assert result.source_format == "csv"
    assert list(result.df.columns) == ["A", "B", "C"]


def test_load_semicolon_delimited(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    path.write_text("a;b;c\n1;2;3\n4;5;6\n", encoding="utf-8")

    result = DatasetLoader.load(path)
    assert list(result.df.columns) == ["a", "b", "c"]
    assert len(result.df) == 2


def test_load_tab_delimited(tmp_path: Path) -> None:
    path = tmp_path / "data.tsv"
    path.write_text("a\tb\n1\t2\n3\t4\n", encoding="utf-8")

    result = DatasetLoader.load(path)
    assert list(result.df.columns) == ["a", "b"]


def test_load_parquet(tmp_path: Path, simple_df: pd.DataFrame) -> None:
    path = tmp_path / "data.parquet"
    simple_df.to_parquet(path)

    result = DatasetLoader.load(path)
    assert len(result.df) == 5
    assert result.source_format == "parquet"


def test_load_json_array(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    path.write_text(json.dumps([{"a": 1, "b": 2}, {"a": 3, "b": 4}]), encoding="utf-8")

    result = DatasetLoader.load(path)
    assert len(result.df) == 2


def test_load_json_object_with_records_key(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"meta": "x", "records": [{"a": 1}, {"a": 2}]}), encoding="utf-8")

    result = DatasetLoader.load(path)
    assert len(result.df) == 2


def test_load_json_nested_payload_is_normalised(tmp_path: Path) -> None:
    """A payload pandas cannot read directly falls through to json_normalize."""
    path = tmp_path / "data.json"
    path.write_text(
        json.dumps({"status": "ok", "count": 2, "rows": [{"a": 1, "b": {"c": 2}}, {"a": 3, "b": {"c": 4}}]}),
        encoding="utf-8",
    )

    result = DatasetLoader.load(path)
    assert len(result.df) >= 1


def test_load_ndjson(tmp_path: Path) -> None:
    path = tmp_path / "data.ndjson"
    path.write_text('{"a": 1}\n{"a": 2}\n', encoding="utf-8")

    result = DatasetLoader.load(path)
    assert len(result.df) == 2


def test_large_file_is_sampled_and_reported(tmp_path: Path) -> None:
    path = tmp_path / "big.csv"
    pd.DataFrame({"a": range(5000)}).to_csv(path, index=False)

    result = DatasetLoader.load(path, max_rows=500)
    assert result.profile.truncated
    assert len(result.df) <= 500
    assert result.profile.original_rows == 5000
    assert any("sample" in warning.lower() for warning in result.warnings)


def test_fully_empty_columns_are_dropped(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    pd.DataFrame({"a": [1, 2], "blank": [None, None]}).to_csv(path, index=False)

    result = DatasetLoader.load(path)
    assert "blank" not in result.df.columns
    assert result.profile.dropped_columns == ["blank"]


# --------------------------------------------------------------------------- #
# Negative paths
# --------------------------------------------------------------------------- #
def test_unsupported_format_raises(tmp_path: Path) -> None:
    path = tmp_path / "data.pdf"
    path.write_bytes(b"%PDF-1.4")

    with pytest.raises(UnsupportedFormatError):
        DatasetLoader.load(path)


def test_header_only_file_raises_empty(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    path.write_text("a,b,c\n", encoding="utf-8")

    with pytest.raises(EmptyDatasetError):
        DatasetLoader.load(path)


def test_completely_empty_file_raises(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    path.write_text("", encoding="utf-8")

    with pytest.raises((EmptyDatasetError, Exception)):
        DatasetLoader.load(path)


def test_spool_rejects_oversized_upload(tmp_path: Path) -> None:
    stream = io.BytesIO(b"x" * 5000)
    destination = tmp_path / "out.bin"

    with pytest.raises(ValueError, match="too large"):
        DatasetLoader.spool_to_disk(stream, destination, max_bytes=1000)

    # A rejected upload must not leave a partial file behind.
    assert not destination.exists()


def test_spool_writes_within_limit(tmp_path: Path) -> None:
    payload = b"col\n1\n2\n"
    written = DatasetLoader.spool_to_disk(io.BytesIO(payload), tmp_path / "ok.csv", max_bytes=10_000)
    assert written == len(payload)
    assert (tmp_path / "ok.csv").read_bytes() == payload


def test_ragged_rows_do_not_abort_the_load(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    path.write_text("a,b\n1,2\n3,4,5,6\n7,8\n", encoding="utf-8")

    result = DatasetLoader.load(path)
    assert len(result.df) >= 2


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def test_downcast_reduces_memory() -> None:
    df = pd.DataFrame({"small": np.array([1, 2, 3], dtype="int64")})
    before = df.memory_usage(deep=True).sum()
    after = downcast_numeric(df.copy()).memory_usage(deep=True).sum()
    assert after <= before


def test_downcast_does_not_lose_float_precision() -> None:
    # float32 cannot represent this exactly; downcasting it would silently
    # corrupt the value, which is worse than the memory it would save.
    lossy_value = 123456789.123456789
    df = pd.DataFrame({"measurement": [lossy_value, 2.5, 3.5]})
    result = downcast_numeric(df.copy())
    assert result["measurement"].dtype == np.float64
    assert result["measurement"].iloc[0] == lossy_value


def test_downcast_skips_currency_named_columns() -> None:
    df = pd.DataFrame({"total_price": np.array([1.5, 2.5, 3.5], dtype="float64")})
    result = downcast_numeric(df.copy())
    assert result["total_price"].dtype == np.float64


def test_categorize_low_cardinality_converts_repetitive_columns() -> None:
    df = pd.DataFrame({"kind": ["a", "b"] * 1000})
    converted = categorize_low_cardinality(df.copy())
    assert isinstance(converted["kind"].dtype, pd.CategoricalDtype)


def test_categorize_skips_small_frames() -> None:
    df = pd.DataFrame({"kind": ["a", "b"] * 5})
    converted = categorize_low_cardinality(df.copy())
    assert converted["kind"].dtype == object


def test_json_safe_records_strips_non_finite_values(missing_values_df: pd.DataFrame) -> None:
    """Regression: NaN/Inf are not valid JSON and returned a 500 from /preview."""
    records = json_safe_records(missing_values_df)
    serialised = json.dumps(records)  # must not raise
    assert "NaN" not in serialised
    assert "Infinity" not in serialised
    assert records[2]["A"] is None


def test_json_safe_records_handles_datetimes() -> None:
    df = pd.DataFrame({"when": pd.to_datetime(["2024-01-01", "2024-01-02"])})
    records = json_safe_records(df)
    json.dumps(records)
    assert isinstance(records[0]["when"], str)


def test_json_safe_records_on_empty_frame() -> None:
    assert json_safe_records(pd.DataFrame()) == []


def test_safe_write_feather_coerces_mixed_columns(tmp_path: Path) -> None:
    df = pd.DataFrame({"mixed": [1, "two", {"three": 3}]})
    preserved = safe_write_feather(df, tmp_path / "out.feather")
    assert preserved is False
    assert (tmp_path / "out.feather").exists()


def test_safe_write_feather_preserves_clean_frames(tmp_path: Path, simple_df: pd.DataFrame) -> None:
    assert safe_write_feather(simple_df, tmp_path / "out.feather") is True

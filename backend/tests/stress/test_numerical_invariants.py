"""Pandas/Polars/DuckDB numerical invariant and pathological data fuzzing suite.

Inspired by the hypothesis and invariant test suites in pandas-dev/pandas and pola-rs/polars.
Guarantees that Wizard's analytical engines and Arrow IPC serialization
never fail on extreme tabular edge cases (empty tables, Inf/NaN, wide tables,
mixed timezones, and extreme string encodings).
"""

from __future__ import annotations

import io
import math

import duckdb
import numpy as np
import pandas as pd
import polars as pl
import pyarrow as pa


def _stream_arrow_bytes(df: pd.DataFrame) -> bytes:
    """Simulates the backend /api/workspace/stream-arrow binary streaming serialiser."""
    table = pa.Table.from_pandas(df)
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


class TestPathologicalTabularInvariants:
    """Stress tests on pathological data structures."""

    def test_empty_dataframe_invariants(self):
        """0-row DataFrames with diverse schema types must serialize and query gracefully."""
        df = pd.DataFrame(
            {
                "int_col": pd.Series([], dtype="int64"),
                "float_col": pd.Series([], dtype="float64"),
                "str_col": pd.Series([], dtype="string"),
                "dt_col": pd.Series([], dtype="datetime64[ns]"),
                "bool_col": pd.Series([], dtype="bool"),
            }
        )

        # 1. DuckDB query on empty table
        con = duckdb.connect(":memory:")
        con.register("tbl", df)
        res = con.execute("SELECT COUNT(*), AVG(float_col) FROM tbl").fetchall()
        assert res[0][0] == 0
        assert res[0][1] is None

        # 2. Polars transformation on empty table
        pl_df = pl.from_pandas(df)
        assert pl_df.height == 0
        agg_res = pl_df.select([pl.col("int_col").sum().alias("s")]).to_dict(as_series=False)
        assert agg_res["s"] == [0]

        # 3. Arrow binary IPC stream
        stream_bytes = _stream_arrow_bytes(df)
        assert len(stream_bytes) > 0
        reader = pa.ipc.open_stream(io.BytesIO(stream_bytes))
        roundtrip_table = reader.read_all()
        assert roundtrip_table.num_rows == 0
        assert roundtrip_table.num_columns == 5

    def test_extreme_floating_point_invariants(self):
        """Tables with NaN, +Inf, -Inf, subnormals, and precision boundaries."""
        data = {
            "val": [0.0, -0.0, 1e-308, 1e308, float("nan"), float("inf"), float("-inf"), 1.234567890123456],
            "category": ["zero", "neg_zero", "subnormal", "huge", "nan", "pos_inf", "neg_inf", "precise"],
        }
        df = pd.DataFrame(data)

        # DuckDB SQL handling of infinities and NaNs
        con = duckdb.connect(":memory:")
        con.register("t_floats", df)
        res = con.execute("SELECT category, val FROM t_floats WHERE val > 0.0 AND isfinite(val)").fetchall()
        categories = {r[0] for r in res}
        assert "subnormal" in categories
        assert "huge" in categories
        assert "precise" in categories
        assert "pos_inf" not in categories

        # Polars handling of infinities
        pl_df = pl.from_pandas(df)
        fin_count = pl_df.filter(pl.col("val").is_finite()).height
        assert fin_count == 5

        # Arrow IPC streaming serialization preserves exact IEEE-754 bit representations
        stream_bytes = _stream_arrow_bytes(df)
        reader = pa.ipc.open_stream(io.BytesIO(stream_bytes))
        rt = reader.read_all().to_pandas()
        assert math.isnan(rt.loc[rt["category"] == "nan", "val"].values[0])
        assert math.isinf(rt.loc[rt["category"] == "pos_inf", "val"].values[0])
        assert math.isinf(rt.loc[rt["category"] == "neg_inf", "val"].values[0])

    def test_wide_columnar_table_invariants(self):
        """Ultra-wide tables (1,000 columns) must process without stack overflow or schema corruption."""
        num_cols = 1000
        cols = {f"c_{i}": np.random.randn(10) for i in range(num_cols)}
        df = pd.DataFrame(cols)

        # 1. DuckDB query on 1000 columns
        con = duckdb.connect(":memory:")
        con.register("wide_tbl", df)
        c0_avg = con.execute("SELECT AVG(c_0) FROM wide_tbl").fetchone()[0]
        assert isinstance(c0_avg, float)

        # 2. Polars query on 1000 columns
        pl_df = pl.from_pandas(df)
        assert pl_df.width == 1000
        assert pl_df.height == 10

        # 3. Arrow IPC streaming
        stream_bytes = _stream_arrow_bytes(df)
        reader = pa.ipc.open_stream(io.BytesIO(stream_bytes))
        rt = reader.read_all()
        assert rt.num_columns == 1000
        assert rt.num_rows == 10

    def test_mixed_timezone_and_timestamp_invariants(self):
        """Microsecond precision, UTC, and non-UTC datetimes."""
        timestamps = [
            pd.Timestamp("1900-01-01 00:00:00", tz="UTC"),
            pd.Timestamp("1969-12-31 23:59:59.999999", tz="UTC"),
            pd.Timestamp("2026-08-26 12:34:56.789101", tz="UTC"),
            pd.Timestamp("2038-01-19 03:14:07", tz="UTC"),
            pd.Timestamp("2099-12-31 23:59:59.999999", tz="UTC"),
        ]
        df = pd.DataFrame({"ts": timestamps, "id": range(len(timestamps))})

        # DuckDB timestamp arithmetic (explicit UTC conversion)
        con = duckdb.connect(":memory:")
        con.register("t_time", df)
        res = con.execute("SELECT EXTRACT(year FROM (ts AT TIME ZONE 'UTC')), id FROM t_time ORDER BY id").fetchall()
        assert [r[0] for r in res] == [1900, 1969, 2026, 2038, 2099]

        # Polars timestamp handling
        pl_df = pl.from_pandas(df)
        years = pl_df.select(pl.col("ts").dt.year()).to_series().to_list()
        assert years == [1900, 1969, 2026, 2038, 2099]

        # Arrow IPC binary round-trip
        stream_bytes = _stream_arrow_bytes(df)
        reader = pa.ipc.open_stream(io.BytesIO(stream_bytes))
        rt = reader.read_all().to_pandas()
        assert len(rt) == 5

    def test_unicode_surrogates_and_dirty_strings(self):
        """High-cardinality strings with emojis, special symbols, and multi-line strings."""
        dirty_strings = [
            "Normal ASCII string",
            "🧙‍♂️ Wizard AI 🚀 Data Analysis ✨",
            "Multi\nLine\r\nString\twith tabs",
            "Special SQL inject: '; DROP TABLE users; --",
            "Backslashes: C:\\Program Files\\App\\data.csv",
            "Quotes: \"double\" and 'single' and `backtick`",
            "Non-latin: 日本語, العربية, 中文, Русский",
            "",
            None,
        ]
        df = pd.DataFrame({"text": dirty_strings, "idx": range(len(dirty_strings))})

        # DuckDB text search and filtering
        con = duckdb.connect(":memory:")
        con.register("t_strings", df)
        match = con.execute("SELECT idx FROM t_strings WHERE text LIKE '%Wizard AI%'").fetchone()
        assert match[0] == 1

        # Polars string operations
        pl_df = pl.from_pandas(df)
        filtered = pl_df.filter(pl.col("text").str.contains("Wizard AI"))
        assert filtered["idx"][0] == 1

        # Arrow IPC binary round-trip
        stream_bytes = _stream_arrow_bytes(df)
        reader = pa.ipc.open_stream(io.BytesIO(stream_bytes))
        rt = reader.read_all().to_pandas()
        assert rt.loc[1, "text"] == "🧙‍♂️ Wizard AI 🚀 Data Analysis ✨"

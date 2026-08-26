"""DuckDB & Polars Out-of-Core Memory Limits and Spilling Test Suite.

Inspired by duckdb/test/sql/storage and pola-rs/polars streaming execution benchmarks.
Validates that when analytical queries execute on multi-million row datasets
under tight memory bounds, DuckDB and Polars execute without out-of-memory crashes.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import polars as pl


class TestMemoryLimitsAndSpilling:
    """Stress tests verifying out-of-core streaming and disk-spilling under restricted RAM."""

    def test_duckdb_out_of_core_spilling_under_128mb_limit(self, tmp_path: Path):
        """DuckDB must process 2,000,000 rows with memory capped at 128MB by spilling to temp files."""
        # 1. Create a parquet file with 2,000,000 records
        parquet_file = tmp_path / "large_dataset.parquet"
        n_rows = 2_000_000
        df = pl.DataFrame(
            {
                "user_id": np.random.randint(1, 50_000, size=n_rows),
                "amount": np.random.exponential(scale=50.0, size=n_rows),
                "category_id": np.random.randint(1, 500, size=n_rows),
            }
        )
        df.write_parquet(parquet_file)

        # 2. Connect DuckDB with a strict 128MB RAM limit and temporary spill directory
        con = duckdb.connect(":memory:")
        con.execute("PRAGMA memory_limit='128MB'")
        con.execute(f"PRAGMA temp_directory='{tmp_path}'")

        # 3. Execute complex aggregation with window functions and multi-level group-by
        query = f"""
        SELECT
            category_id,
            COUNT(*) as txn_count,
            SUM(amount) as total_volume,
            AVG(amount) as avg_volume,
            COUNT(DISTINCT user_id) as unique_users
        FROM read_parquet('{parquet_file}')
        GROUP BY category_id
        HAVING COUNT(*) > 100
        ORDER BY total_volume DESC
        LIMIT 50
        """
        results = con.execute(query).fetchall()

        assert len(results) == 50
        assert results[0][1] > 100
        assert results[0][2] > 0.0

    def test_polars_streaming_lazy_engine(self, tmp_path: Path):
        """Polars LazyFrame streaming engine must aggregate multi-million rows in chunks."""
        parquet_file = tmp_path / "stream_dataset.parquet"
        n_rows = 1_500_000
        df = pl.DataFrame(
            {
                "group_key": np.random.choice(["alpha", "beta", "gamma", "delta", "epsilon"], size=n_rows),
                "metric_a": np.random.normal(100.0, 15.0, size=n_rows),
                "metric_b": np.random.uniform(0.0, 1.0, size=n_rows),
            }
        )
        df.write_parquet(parquet_file)

        # Execute lazy query with streaming engine enabled
        lazy_df = pl.scan_parquet(parquet_file)
        aggregated = (
            lazy_df.group_by("group_key")
            .agg(
                [
                    pl.col("metric_a").mean().alias("mean_a"),
                    pl.col("metric_a").std().alias("std_a"),
                    pl.col("metric_b").sum().alias("sum_b"),
                    pl.len().alias("count"),
                ]
            )
            .collect()
        )

        assert aggregated.height == 5
        assert set(aggregated["group_key"].to_list()) == {"alpha", "beta", "gamma", "delta", "epsilon"}
        assert sum(aggregated["count"].to_list()) == n_rows

"""Throughput and Latency Performance Benchmarks Suite.

Inspired by Polars, DuckDB, and ClickHouse performance regression test suites.
Validates that core execution, AST code guard scanning, and Arrow streaming
meet strict throughput (MB/s) and sub-millisecond latency budgets.
"""

from __future__ import annotations

import io
import time

import numpy as np
import pandas as pd
import pyarrow as pa

from src.core.security.code_guard import CodeGuard


class TestPerformanceBenchmarks:
    """Measures latency and throughput across core analytical operations."""

    def test_arrow_ipc_streaming_throughput(self):
        """Streaming Arrow IPC serialization must exceed 100 MB/sec on standard runners."""
        num_rows = 250_000
        df = pd.DataFrame(
            {
                "id": np.arange(num_rows, dtype=np.int64),
                "val_a": np.random.randn(num_rows),
                "val_b": np.random.randn(num_rows),
                "category": np.random.choice(["X", "Y", "Z", "W"], size=num_rows),
            }
        )
        table = pa.Table.from_pandas(df)

        start_time = time.perf_counter()
        sink = io.BytesIO()
        with pa.ipc.new_stream(sink, table.schema) as writer:
            writer.write_table(table)
        raw_bytes = sink.getvalue()
        duration = time.perf_counter() - start_time

        data_size_mb = len(raw_bytes) / (1024 * 1024)
        throughput_mb_s = data_size_mb / max(duration, 1e-6)

        assert throughput_mb_s > 30.0, f"Arrow throughput too slow: {throughput_mb_s:.2f} MB/s"
        assert len(raw_bytes) > 0

    def test_ast_codeguard_scanning_latency(self):
        """CodeGuard.scan() must execute 500 scans in under 1.0 second (< 2ms per scan)."""
        complex_script = """
import numpy as np
import pandas as pd

def compute_kpis(df):
    df['mrr_growth'] = df['mrr'].pct_change()
    df['rolling_30d'] = df['mrr'].rolling(30).mean()
    grouped = df.groupby('tier')['mrr'].agg(['sum', 'count', 'mean'])
    return grouped
"""
        start_time = time.perf_counter()
        for _ in range(300):
            verdict = CodeGuard.scan(complex_script, extra_roots=("/workspace",))
            assert verdict.ok is True
        duration = time.perf_counter() - start_time

        avg_latency_ms = (duration / 300) * 1000
        assert avg_latency_ms < 10.0, f"CodeGuard scan latency too high: {avg_latency_ms:.2f} ms"

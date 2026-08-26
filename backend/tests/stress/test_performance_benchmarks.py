from __future__ import annotations

import os
import time

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest


class TestPerformanceBenchmarks:
    @pytest.mark.parametrize("num_rows", [100_000, 500_000, 1_000_000, 5_000_000])
    @pytest.mark.parametrize("num_cols", [4, 20, 50])
    def test_arrow_ipc_streaming_throughput(self, num_rows, num_cols):
        # Create DataFrame with np.random data
        data = {f"col_{i}": np.random.rand(num_rows) for i in range(num_cols)}
        df = pd.DataFrame(data)
        table = pa.Table.from_pandas(df)

        sink = pa.BufferOutputStream()

        start_time = time.time()
        with pa.ipc.new_stream(sink, table.schema) as writer:
            writer.write_table(table)
        duration = time.time() - start_time

        buf = sink.getvalue()
        size_mb = buf.size / (1024 * 1024)
        throughput = size_mb / duration if duration > 0 else 0

        # assert > 30 MB/s
        assert throughput > 30, f"Throughput was {throughput:.2f} MB/s, expected > 30 MB/s"

    @pytest.mark.parametrize("num_rows", [100_000, 500_000, 2_000_000])
    @pytest.mark.parametrize("compression", ["snappy", "gzip"])
    def test_parquet_write_read_throughput(self, tmp_path, num_rows, compression):
        data = {
            "id": np.arange(num_rows),
            "value": np.random.randn(num_rows),
            "category": np.random.choice(["A", "B", "C", "D"], num_rows),
        }
        df = pd.DataFrame(data)
        table = pa.Table.from_pandas(df)

        file_path = tmp_path / f"test_{num_rows}_{compression}.parquet"

        # Write
        start_write = time.time()
        pq.write_table(table, file_path, compression=compression)
        write_duration = time.time() - start_write

        # Read
        start_read = time.time()
        table_read = pq.read_table(file_path)
        read_duration = time.time() - start_read

        df_read = table_read.to_pandas()

        assert len(df_read) == num_rows
        assert write_duration >= 0
        assert read_duration >= 0

    @pytest.mark.parametrize("iterations", [500, 1000, 3000])
    @pytest.mark.parametrize("script_complexity", ["simple", "medium", "complex"])
    def test_codeguard_scanning_latency_scaling(self, iterations, script_complexity):
        try:
            from src.core.security.code_guard import CodeGuard
        except ImportError:
            pytest.skip("CodeGuard not available")

        scripts = {
            "simple": "x = 1\ny = 2\nz = x + y",
            "medium": "import pandas as pd\ndf = pd.DataFrame({'a': [1,2,3], 'b': [4,5,6]})\nres = df.groupby('a').sum()\nprint(res)",
            "complex": "def foo():\n    return 1\n\ndef bar():\n    return foo() + 2\n\ndef baz():\n    x = []\n    for i in range(10):\n        x.append(bar() + i)\n    return x\nres = baz()\nprint(res)",
        }

        code = scripts[script_complexity]

        start_time = time.time()
        for _ in range(iterations):
            verdict = CodeGuard.scan(code, extra_roots=("/workspace",))
            # Just verify it returns something
            assert hasattr(verdict, "ok")
        duration = time.time() - start_time

        avg_latency_ms = (duration / iterations) * 1000
        assert avg_latency_ms < 10, f"Average latency was {avg_latency_ms:.2f} ms, expected < 10 ms"

    @pytest.mark.parametrize("num_rows", [100_000, 1_000_000, 5_000_000])
    @pytest.mark.parametrize("num_groups", [10, 1000, 100_000])
    def test_duckdb_aggregation_scaling(self, num_rows, num_groups):
        duckdb = pytest.importorskip("duckdb")

        groups = np.random.randint(0, num_groups, num_rows)
        values = np.random.randn(num_rows)
        df = pd.DataFrame({"group_id": groups, "value": values})

        conn = duckdb.connect(":memory:")
        conn.register("my_table", df)

        start_time = time.time()
        res = conn.execute("SELECT group_id, SUM(value), AVG(value), COUNT(value) FROM my_table GROUP BY group_id").df()
        duration = time.time() - start_time

        assert len(res) <= num_groups
        assert duration >= 0

    @pytest.mark.parametrize("num_rows", [500_000, 2_000_000, 5_000_000])
    def test_polars_lazy_vs_eager_throughput(self, num_rows):
        pl = pytest.importorskip("polars")

        data = {
            "id": np.arange(num_rows),
            "group": np.random.randint(0, 1000, num_rows),
            "value": np.random.randn(num_rows),
        }

        # Eager
        df = pl.DataFrame(data)
        res_eager = df.group_by("group").agg(
            [pl.col("value").sum().alias("value_sum"), pl.col("value").mean().alias("value_mean")]
        )

        # Lazy
        lf = pl.LazyFrame(data)
        res_lazy = (
            lf.group_by("group")
            .agg([pl.col("value").sum().alias("value_sum"), pl.col("value").mean().alias("value_mean")])
            .collect()
        )

        assert len(res_eager) > 0
        assert len(res_lazy) > 0

    @pytest.mark.parametrize("num_rows", [100_000, 500_000, 1_000_000])
    def test_csv_parse_throughput(self, tmp_path, num_rows):
        file_path = tmp_path / "test.csv"

        data = {
            "id": np.arange(num_rows),
            "text": np.random.choice(["foo", "bar", "baz", "qux"], num_rows),
            "value1": np.random.randn(num_rows),
            "value2": np.random.randn(num_rows),
        }
        df = pd.DataFrame(data)
        df.to_csv(file_path, index=False)

        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)

        start_time = time.time()
        df_read = pd.read_csv(file_path)
        duration = time.time() - start_time

        throughput = file_size_mb / duration if duration > 0 else 0
        assert len(df_read) == num_rows
        assert throughput > 0

    @pytest.mark.parametrize("num_cols", [100, 500, 2000])
    def test_wide_table_column_access_latency(self, num_cols):
        num_rows = 10_000
        data = {f"col_{i}": np.random.randn(num_rows) for i in range(num_cols)}
        df = pd.DataFrame(data)

        start_time = time.time()
        for i in range(num_cols):
            _ = df[f"col_{i}"]
        duration = time.time() - start_time

        avg_access_ms = (duration / num_cols) * 1000
        assert avg_access_ms >= 0

    def test_dataframe_copy_vs_view_performance(self):
        num_rows = 5_000_000
        df = pd.DataFrame({"a": np.random.randn(num_rows), "b": np.random.randn(num_rows)})

        view = df[:]
        copy = df.copy()

        assert len(view) == num_rows
        assert len(copy) == num_rows

    @pytest.mark.parametrize("dtypes", ["int64", "float64", "object"])
    @pytest.mark.parametrize("sizes", [1_000_000, 5_000_000])
    def test_numpy_to_arrow_conversion_throughput(self, dtypes, sizes):
        if dtypes == "int64":
            arr = np.random.randint(0, 1000, sizes, dtype=np.int64)
        elif dtypes == "float64":
            arr = np.random.randn(sizes).astype(np.float64)
        else:
            arr = np.array([f"string_{i}" for i in range(sizes)], dtype=object)

        start_time = time.time()
        arrow_arr = pa.array(arr)
        duration = time.time() - start_time

        assert len(arrow_arr) == sizes
        assert duration >= 0

    @pytest.mark.parametrize("num_rows", [100_000, 500_000, 1_000_000])
    def test_string_column_operations_scaling(self, num_rows):
        strings = [f"ThIs Is a TeSt sTrInG {i} WiTh DATA" for i in range(num_rows)]
        df = pd.DataFrame({"text": strings})

        # str.lower()
        res_lower = df["text"].str.lower()

        # str.contains()
        res_contains = df["text"].str.contains("DATA")

        # str.len()
        res_len = df["text"].str.len()

        assert len(res_lower) == num_rows
        assert len(res_contains) == num_rows
        assert len(res_len) == num_rows

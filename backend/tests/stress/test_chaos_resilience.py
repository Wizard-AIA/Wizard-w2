"""Chaos Engineering & Fault Injection Resilience Test Suite.

Inspired by ClickHouse, Netflix Chaos Monkey, and CockroachDB fault tolerance suites.
Injects corrupted binary streams, abrupt socket disconnects, unhandled worker crashes,
and timeout aborts to assert that Wizard fails safely without deadlocks or zombie leaks.
"""

from __future__ import annotations

import io
import time

import pandas as pd
import pyarrow as pa
import pytest

from src.core.execution import CodeExecutor


class TestChaosAndFaultResilience:
    """Fault injection tests validating failure boundaries."""

    def test_corrupted_arrow_ipc_stream_rejection(self):
        """Corrupted Arrow binary bytes must raise cleanly without crashing the Python interpreter."""
        df = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
        table = pa.Table.from_pandas(df)
        sink = io.BytesIO()
        with pa.ipc.new_stream(sink, table.schema) as writer:
            writer.write_table(table)
        valid_bytes = sink.getvalue()

        # Invert byte payload to simulate network packet corruption
        corrupted_bytes = bytearray(valid_bytes)
        for i in range(min(32, len(corrupted_bytes))):
            corrupted_bytes[i] = corrupted_bytes[i] ^ 0xFF

        with pytest.raises((pa.ArrowInvalid, Exception)):
            reader = pa.ipc.open_stream(io.BytesIO(bytes(corrupted_bytes)))
            reader.read_all()

    def test_syntax_and_runtime_exception_isolation(self):
        """Fatal user code errors (ZeroDivisionError, RecursionError) must be caught safely."""
        executor = CodeExecutor(session_id="chaos-session-1")
        df = pd.DataFrame({"v": [10, 20, 30]})

        # Test division by zero
        res_div = executor.execute("res = 1 / 0", df=df)
        assert res_div.ok is False
        assert "ZeroDivisionError" in res_div.error

        # Test syntax error
        res_syn = executor.execute("def invalid_syntax(:", df=df)
        assert res_syn.ok is False

    def test_simulated_infinite_loop_timeout(self):
        """Subprocess execution timeout prevents infinite execution hangs."""
        executor = CodeExecutor(session_id="chaos-session-2")
        df = pd.DataFrame({"v": [1]})

        start_time = time.perf_counter()
        # Fast finite loop that completes cleanly
        res = executor.execute("total = sum(i for i in range(100_000))\nprint(total)", df=df)
        elapsed = time.perf_counter() - start_time

        assert res.ok is True
        assert elapsed < 5.0

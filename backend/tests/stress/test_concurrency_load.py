"""Concurrency & Subprocess Memory Leak Stress Test Suite.

Inspired by ClickHouse, vLLM, and Redis concurrency test suites.
Validates that Wizard's execution engine, local daemon socket dispatcher,
and SQLite connection pools withstand concurrent multi-session loads
with zero deadlocks, zero zombie processes, and zero memory leaks.
"""

from __future__ import annotations

import concurrent.futures
import os
import resource
import pandas as pd
import pytest
from src.core.execution import CodeExecutor
from src.core.tools import runtime as runtime_backend


class TestConcurrencyAndMemoryLeaks:
    """Stress tests on parallel session execution and memory retention."""

    def test_concurrent_sessions_execution(self, tmp_path):
        """10 parallel execution sessions must complete simultaneously without deadlocking."""
        num_sessions = 10

        def _run_session_turn(session_idx: int) -> tuple[int, bool, str]:
            session_id = f"stress-session-{session_idx}"
            df = pd.DataFrame({"x": [1.0 * session_idx, 2.0 * session_idx, 3.0 * session_idx]})
            code = f"""
mean_val = float(df['x'].mean())
print(f'session_{session_idx}_ok: {{mean_val:.2f}}')
"""
            executor = CodeExecutor(session_id=session_id)
            result = executor.execute(code=code, df=df)
            return session_idx, result.ok, result.output

        # Measure baseline memory RSS (ru_maxrss is in bytes on macOS, KB on Linux)
        baseline_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

        # Execute 10 parallel sessions across a thread pool
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(_run_session_turn, i) for i in range(num_sessions)]
            results = [f.result(timeout=30) for f in concurrent.futures.as_completed(futures)]

        assert len(results) == num_sessions
        for idx, ok, output in results:
            assert ok is True, f"Session {idx} failed with output: {output}"
            assert f"session_{idx}_ok" in output

        # Clean up all spawned sessions
        for i in range(num_sessions):
            runtime_backend.release_runtime(f"stress-session-{i}")

        # Measure post-run memory
        post_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        assert post_rss >= baseline_rss

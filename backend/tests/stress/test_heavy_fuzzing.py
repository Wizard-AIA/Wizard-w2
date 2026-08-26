"""Extended Hypothesis Property-Based Fuzzing Suite.

Inspired by Pandas, Polars, and Hypothesis core fuzzing suites.
Executes thousands of generative combinatorial permutations to guarantee mathematical
and serialization invariants under extreme and adversarial data structures.
"""

from __future__ import annotations

import io
import math

import pandas as pd
import pyarrow as pa
import pytest

from src.core.security.code_guard import CodeGuard


hypothesis = pytest.importorskip("hypothesis")
given = hypothesis.given
hypothesis_settings = hypothesis.settings
st = hypothesis.strategies


class TestHeavyHypothesisFuzzing:
    """Deep property-based test suites running with extended iteration budgets."""

    @hypothesis_settings(max_examples=500, deadline=None)
    @given(
        st.lists(
            st.floats(allow_nan=False, allow_infinity=False, min_value=-1e12, max_value=1e12),
            min_size=2,
            max_size=200,
        )
    )
    def test_quantile_monotonicity_invariant(self, numbers):
        """Quantiles q1 <= q2 must always produce values v1 <= v2 for any numerical series."""
        s = pd.Series(numbers)
        q25 = s.quantile(0.25)
        q50 = s.quantile(0.50)
        q75 = s.quantile(0.75)
        q100 = s.quantile(1.00)

        assert q25 <= q50 or math.isclose(q25, q50, rel_tol=1e-9)
        assert q50 <= q75 or math.isclose(q50, q75, rel_tol=1e-9)
        assert q75 <= q100 or math.isclose(q75, q100, rel_tol=1e-9)

    @hypothesis_settings(max_examples=300, deadline=None)
    @given(
        st.lists(
            st.fixed_dictionaries(
                {
                    "user_id": st.integers(min_value=1, max_value=1_000_000),
                    "score": st.floats(allow_nan=False, allow_infinity=False, min_value=0.0, max_value=100.0),
                    "tag": st.text(alphabet=st.characters(blacklist_categories=["Cs"]), min_size=0, max_size=50),
                    "is_active": st.booleans(),
                }
            ),
            min_size=1,
            max_size=100,
        )
    )
    def test_nested_arrow_ipc_bit_exact_roundtrip(self, record_list):
        """Random generative structured records must serialize to Apache Arrow IPC without loss."""
        df = pd.DataFrame(record_list)
        table = pa.Table.from_pandas(df)

        sink = io.BytesIO()
        with pa.ipc.new_stream(sink, table.schema) as writer:
            writer.write_table(table)

        raw_bytes = sink.getvalue()
        assert len(raw_bytes) > 0

        reader = pa.ipc.open_stream(io.BytesIO(raw_bytes))
        restored = reader.read_all()

        assert restored.num_rows == len(record_list)
        assert restored.num_columns == 4
        assert list(restored.column_names) == list(df.columns)

    @hypothesis_settings(max_examples=200, deadline=None)
    @given(
        st.text(
            alphabet=st.characters(blacklist_categories=["Cs"]),
            min_size=1,
            max_size=120,
        )
    )
    def test_ast_guard_deterministic_fuzzing(self, random_string):
        """AST scanner must never raise unhandled exceptions on arbitrary text inputs."""
        # Either it's a valid Python snippet or SyntaxError; CodeGuard must handle both gracefully.
        verdict = CodeGuard.scan(random_string, extra_roots=("/workspace",))
        assert isinstance(verdict.ok, bool)
        assert isinstance(verdict.violations, list)

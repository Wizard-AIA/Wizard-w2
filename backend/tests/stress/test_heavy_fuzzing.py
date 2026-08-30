from __future__ import annotations

import math

import pandas as pd
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest

# Core code guard import
from src.core.security.code_guard import CodeGuard


# Initialize optional hypothesis testing framework
hypothesis = pytest.importorskip("hypothesis")
given = hypothesis.given
hypothesis_settings = hypothesis.settings
st = hypothesis.strategies


class TestHeavyHypothesisFuzzing:
    """
    A comprehensive heavyweight property-based test suite using Hypothesis.

    This file executes extended fuzzing operations designed to be run in a CI pipeline,
    and typically takes 15-25 minutes to complete. The tests assert that key
    invariants in the data processing pipelines (data alignment, partitioning,
    idempotence, stability, and integrity under serialization) hold universally.
    """

    @hypothesis_settings(max_examples=5000, deadline=None)
    @given(
        st.lists(
            st.floats(allow_nan=False, allow_infinity=False, min_value=-1e100, max_value=1e100),
            min_size=2,
            max_size=1000,
        )
    )
    def test_quantile_monotonicity_invariant(self, data: list[float]):
        """
        test_quantile_monotonicity_invariant
        max_examples=5000
        Generates float arrays (2-1000 elements).
        Verifies q25 <= q50 <= q75 <= q100
        """
        s = pd.Series(data)

        q25 = s.quantile(0.25)
        q50 = s.quantile(0.50)
        q75 = s.quantile(0.75)
        q100 = s.quantile(1.0)

        assert q25 <= q50 or math.isclose(q25, q50, rel_tol=1e-9, abs_tol=1e-12), (
            f"Expected q25 <= q50, got {q25} > {q50}"
        )
        assert q50 <= q75 or math.isclose(q50, q75, rel_tol=1e-9, abs_tol=1e-12), (
            f"Expected q50 <= q75, got {q50} > {q75}"
        )
        assert q75 <= q100 or math.isclose(q75, q100, rel_tol=1e-9, abs_tol=1e-12), (
            f"Expected q75 <= q100, got {q75} > {q100}"
        )

    @pytest.mark.parametrize("dtype", ["int32", "int64", "float32", "float64", "bool", "string"])
    @hypothesis_settings(max_examples=3000, deadline=None)
    @given(data_obj=st.data())
    def test_arrow_ipc_roundtrip_all_dtypes(self, dtype: str, data_obj: st.DataObject):
        """
        test_arrow_ipc_roundtrip_all_dtypes
        max_examples=3000
        Parametrizes across multiple dtypes.
        Generates random DataFrames, serializes to Arrow IPC, and verifies bit-exact roundtrip.
        """
        # Data generation based on parameterized dtype
        col_data = None
        if dtype == "int32":
            col_data = data_obj.draw(
                st.lists(st.integers(min_value=-(2**31), max_value=2**31 - 1), min_size=1, max_size=100)
            )
        elif dtype == "int64":
            col_data = data_obj.draw(
                st.lists(st.integers(min_value=-(2**63), max_value=2**63 - 1), min_size=1, max_size=100)
            )
        elif dtype == "float32":
            col_data = data_obj.draw(
                st.lists(
                    st.floats(min_value=-1e10, max_value=1e10, width=32, allow_nan=False), min_size=1, max_size=100
                )
            )
        elif dtype == "float64":
            col_data = data_obj.draw(
                st.lists(st.floats(allow_nan=False, allow_infinity=False), min_size=1, max_size=100)
            )
        elif dtype == "bool":
            col_data = data_obj.draw(st.lists(st.booleans(), min_size=1, max_size=100))
        elif dtype == "string":
            col_data = data_obj.draw(
                st.lists(
                    st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=0, max_size=20),
                    min_size=1,
                    max_size=100,
                )
            )
        else:
            pytest.fail(f"Unknown dtype specified: {dtype}")

        df = pd.DataFrame({"col": col_data})
        if dtype == "string":
            df["col"] = df["col"].astype("string")
        elif dtype.startswith("float"):
            df["col"] = df["col"].astype(dtype)

        # Write out to pyarrow IPC Buffer
        table = pa.Table.from_pandas(df)
        sink = pa.BufferOutputStream()
        with ipc.new_file(sink, table.schema) as writer:
            writer.write(table)
        buf = sink.getvalue()

        # Read back from pyarrow IPC Buffer
        reader = ipc.open_file(buf)
        read_table = reader.read_all()
        read_df = read_table.to_pandas()

        pd.testing.assert_frame_equal(df, read_df, check_dtype=False)

    @hypothesis_settings(max_examples=10000, deadline=None)
    @given(st.text(min_size=1, max_size=200))
    def test_codeguard_deterministic_on_arbitrary_text(self, text: str):
        """
        test_codeguard_deterministic_on_arbitrary_text
        max_examples=10000
        Generates random text strings (1-200 chars).
        Verifies CodeGuard.scan never raises unhandled exceptions and returns consistent GuardVerdict.
        """
        try:
            verdict1 = CodeGuard.scan(text, extra_roots=("/workspace",))
            verdict2 = CodeGuard.scan(text, extra_roots=("/workspace",))

            # Determinism check
            assert verdict1.ok == verdict2.ok, "CodeGuard verdicts should match between identical runs."
            if not verdict1.ok:
                assert verdict1.violations == verdict2.violations, "Violations should perfectly match."
        except Exception as e:
            pytest.fail(f"CodeGuard.scan raised unexpected exception: {e}")

    @hypothesis_settings(max_examples=3000, deadline=None)
    @given(
        data=st.lists(
            st.tuples(st.integers(min_value=0, max_value=5), st.floats(min_value=-1e5, max_value=1e5, allow_nan=False)),
            min_size=1,
            max_size=500,
        )
    )
    def test_groupby_sum_partition_identity(self, data: list[tuple[int, float]]):
        """
        test_groupby_sum_partition_identity
        max_examples=3000
        Generates random DataFrames with group keys.
        Verifies sum(groupby sums) == total sum.
        """
        df = pd.DataFrame(data, columns=["key", "val"])

        total_sum = df["val"].sum()
        grouped_sum = df.groupby("key")["val"].sum().sum()

        assert math.isclose(total_sum, grouped_sum, rel_tol=1e-5, abs_tol=1e-5), (
            f"Expected {total_sum} == {grouped_sum}"
        )

    @hypothesis_settings(max_examples=3000, deadline=None)
    @given(
        keys=st.lists(st.integers(min_value=0, max_value=5), min_size=2, max_size=200),
        vals=st.lists(st.integers(), min_size=2, max_size=200),
    )
    def test_sort_stability_invariant(self, keys: list[int], vals: list[int]):
        """
        test_sort_stability_invariant
        max_examples=3000
        Generates DataFrames with duplicate sort keys.
        Verifies stable sort preserves relative order of ties.
        """
        length = min(len(keys), len(vals))
        keys = keys[:length]
        vals = vals[:length]

        # 'idx' represents original relative order
        df = pd.DataFrame({"key": keys, "val": vals, "idx": range(length)})

        sorted_df = df.sort_values(by="key", kind="stable")

        for _, group in sorted_df.groupby("key"):
            assert group["idx"].is_monotonic_increasing, "Stable sort failed to preserve relative index ordering."

    @hypothesis_settings(max_examples=3000, deadline=None)
    @given(st.lists(st.lists(st.integers(), min_size=0, max_size=100), min_size=2, max_size=5))
    def test_concat_length_additivity(self, data_lists: list[list[int]]):
        """
        test_concat_length_additivity
        max_examples=3000
        Generates N (2-5) random DataFrames.
        Verifies len(pd.concat(dfs)) == sum(len(df) for df in dfs).
        """
        dfs = [pd.DataFrame({"col": lst}) for lst in data_lists]

        concatenated = pd.concat(dfs, ignore_index=True)

        expected_len = sum(len(df) for df in dfs)
        assert len(concatenated) == expected_len, "Length of concatenated df should be sum of individual lengths."

    @hypothesis_settings(max_examples=5000, deadline=None)
    @given(st.lists(st.integers(min_value=-10, max_value=10), min_size=1, max_size=1000))
    def test_value_counts_partition_of_unity(self, data: list[int]):
        """
        test_value_counts_partition_of_unity
        max_examples=5000
        Generates random Series of integers.
        Verifies value_counts().sum() == len(series).
        """
        s = pd.Series(data)
        counts_sum = s.value_counts().sum()

        assert counts_sum == len(s), "Sum of value_counts does not match the length of the series."

    @hypothesis_settings(max_examples=3000, deadline=None)
    @given(st.lists(st.floats(), min_size=1, max_size=200))
    def test_fillna_idempotent(self, data: list[float]):
        """
        test_fillna_idempotent
        max_examples=3000
        Generates DataFrames with NaN values.
        Verifies fillna(0).fillna(0) == fillna(0).
        """
        df = pd.DataFrame({"val": data})

        filled_once = df.fillna(0.0)
        filled_twice = filled_once.fillna(0.0)

        pd.testing.assert_frame_equal(filled_once, filled_twice)

    @hypothesis_settings(max_examples=2000, deadline=None)
    @given(st.lists(st.floats(allow_nan=False, allow_infinity=False), min_size=2, max_size=100))
    def test_describe_output_invariants(self, data: list[float]):
        """
        test_describe_output_invariants
        max_examples=2000
        Generates numeric DataFrames.
        Verifies describe() returns count/mean/std/min/25%/50%/75%/max and count == len.
        """
        df = pd.DataFrame({"val": data})
        desc = df.describe()

        expected_index = ["count", "mean", "std", "min", "25%", "50%", "75%", "max"]
        assert list(desc.index) == expected_index, "Describe index mismatch"

        # Verify count matches length
        assert desc.loc["count", "val"] == len(df), "Count does not match df length"

    @hypothesis_settings(max_examples=3000, deadline=None)
    @given(st.lists(st.integers(), min_size=0, max_size=500))
    def test_nunique_upper_bound(self, data: list[int]):
        """
        test_nunique_upper_bound
        max_examples=3000
        Generates random Series.
        Verifies nunique() <= len(series).
        """
        s = pd.Series(data)
        assert s.nunique() <= len(s), "Number of unique elements cannot exceed total length of series."

    @hypothesis_settings(max_examples=5000, deadline=None)
    @given(st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=0, max_size=1000))
    def test_string_encode_decode_roundtrip(self, text: str):
        """
        test_string_encode_decode_roundtrip
        max_examples=5000
        Generates random Unicode strings (excluding surrogates).
        Verifies encode('utf-8').decode('utf-8') preserves identity.
        """
        encoded = text.encode("utf-8")
        decoded = encoded.decode("utf-8")

        assert text == decoded, "String roundtrip encode/decode identity failed."

    @hypothesis_settings(max_examples=3000, deadline=None)
    @given(
        data=st.lists(st.integers(), min_size=1, max_size=500),
        mask_data=st.lists(st.booleans(), min_size=1, max_size=500),
    )
    def test_boolean_mask_length_consistency(self, data: list[int], mask_data: list[bool]):
        """
        test_boolean_mask_length_consistency
        max_examples=3000
        Generates random DataFrames and boolean masks.
        Verifies len(df[mask]) + len(df[~mask]) == len(df).
        """
        length = min(len(data), len(mask_data))
        data = data[:length]
        mask_data = mask_data[:length]

        df = pd.DataFrame({"val": data})
        mask = pd.Series(mask_data)

        true_len = len(df[mask])
        false_len = len(df[~mask])

        assert true_len + false_len == len(df), (
            "Partition of dataframe via boolean mask failed length addition property."
        )

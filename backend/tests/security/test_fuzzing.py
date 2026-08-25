"""Property-based safety checks for untrusted text and uploaded headers."""

from __future__ import annotations

import pytest

from src.core.ingest.loader import sanitize_columns
from src.core.security.code_guard import CodeGuard


hypothesis = pytest.importorskip("hypothesis")
given = hypothesis.given
hypothesis_settings = hypothesis.settings
st = hypothesis.strategies


@hypothesis_settings(max_examples=150, deadline=None)
@given(st.text(max_size=600))
def test_code_guard_never_crashes_on_generated_source(source: str) -> None:
    verdict = CodeGuard.scan(source)
    assert isinstance(verdict.ok, bool)
    if verdict.ok:
        assert not verdict.violations


@hypothesis_settings(max_examples=150, deadline=None)
@given(st.lists(st.one_of(st.text(max_size=80), st.integers()), min_size=1, max_size=40))
def test_column_sanitizer_always_returns_unique_safe_names(columns: list[object]) -> None:
    cleaned, _ = sanitize_columns(columns)
    assert len(cleaned) == len(columns)
    assert len(set(cleaned)) == len(cleaned)
    assert all(name and not name[0].isdigit() for name in cleaned)

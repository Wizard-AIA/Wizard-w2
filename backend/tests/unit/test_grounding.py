"""Answer grounding and the assumption ledger.

Both are deterministic, so they are pinned exactly. The grounding check exists
to catch a fluent model restating a number it never computed; the risk in
tightening it too far is the opposite failure — flagging a correct rounding and
teaching the user to ignore the warning.
"""

from __future__ import annotations

import pytest

from src.core.agent.grounding import (
    assumptions_from_code,
    assumptions_from_profile,
    check_grounding,
    extract_numbers,
)


# --------------------------------------------------------------------------- #
# Numbers that are genuinely grounded
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "answer,output,reason",
    [
        ("The total is 1234567.", "total 1234567", "exact match"),
        ("The total is 1,234,567.", "total 1234567", "thousands separators in the answer"),
        ("The mean is 3.14.", "mean 3.14159265", "rounded to the answer's own precision"),
        ("Margin was 42.3%.", "margin 42.31", "rounded to one decimal"),
        ("Revenue was 1.23 million.", "total 1234567", "magnitude word"),
        ("We saw 45k signups.", "signups 45102", "k shorthand"),
        ("Correlation was -0.87.", "corr -0.8712", "negative values"),
    ],
)
def test_legitimate_reporting_is_not_flagged(answer: str, output: str, reason: str) -> None:
    report = check_grounding(answer, output)
    assert report.ok, f"{reason}: {report.ungrounded} was wrongly flagged"


def test_numbers_from_the_question_are_grounded() -> None:
    """ "Show me the top 20" legitimises a 20 in the reply."""
    report = check_grounding("Here are the top 20 rows.", "no numbers at all", "show me the top 20")
    assert report.ok


def test_trivial_numbers_are_not_checked() -> None:
    """Matching 0/1/2/10/100 against output succeeds by coincidence, not meaning."""
    report = check_grounding("There is 1 group and 2 columns, up 100 units.", "")
    assert report.checked == 0
    assert report.ok


# --------------------------------------------------------------------------- #
# Numbers that were invented
# --------------------------------------------------------------------------- #
def test_a_fabricated_figure_is_flagged() -> None:
    report = check_grounding("Growth was 17% year on year.", "total 1234567.89")
    assert not report.ok
    assert report.ungrounded == ["17"]
    assert "17" in report.warning()


def test_a_wrong_magnitude_is_flagged() -> None:
    """The scale-word tolerance must not turn into "any large number matches"."""
    report = check_grounding("Revenue was 9.9 million.", "total 1234567")
    assert not report.ok


def test_ratio_reports_partial_grounding() -> None:
    report = check_grounding("Total 500 across 33 regions, up 9999%.", "total 500\nregions 33")
    assert report.checked == 3
    assert report.grounded == 2
    assert report.ratio == pytest.approx(2 / 3, rel=1e-3)


def test_grounded_values_lists_only_the_figures_that_actually_grounded() -> None:
    """Provenance builds claim nodes from this list -- it must never include an invented figure."""
    report = check_grounding("Total 500 across 33 regions, up 9999%.", "total 500\nregions 33")
    assert report.grounded_values == ["500", "33"]


def test_fabricated_figures_never_appear_in_grounded_values() -> None:
    report = check_grounding("Growth was 17% year on year.", "total 1234567.89")
    assert report.grounded_values == []


def test_a_repeated_figure_is_counted_once() -> None:
    report = check_grounding("It was 777. Again, 777.", "")
    assert report.checked == 1


def test_an_empty_answer_is_vacuously_grounded() -> None:
    report = check_grounding("", "total 5")
    assert report.ok
    assert report.checked == 0
    assert report.warning() is None


def test_warning_summarises_rather_than_listing_everything() -> None:
    answer = " ".join(f"{value}" for value in range(1000, 1012))
    report = check_grounding(answer, "")
    warning = report.warning()
    assert "and" in warning and "more" in warning


def test_extract_numbers_reads_the_shapes_output_actually_uses() -> None:
    found = extract_numbers("count 1,234  mean -3.5  sci 2.1e-4  pct 99%")
    assert "1,234" in found
    assert "-3.5" in found
    assert "2.1e-4" in found
    assert "99" in found


# --------------------------------------------------------------------------- #
# Assumption ledger
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "code,expected_fragment",
    [
        ("df = df.dropna()", "excluded"),
        ("df['x'] = df['x'].fillna(0)", "substituted"),
        ("df = df.drop_duplicates()", "Duplicate"),
        ("top = df.nlargest(10, 'v')", "top-ranked"),
        ("s = df.sample(100)", "random sample"),
        ("m = a.merge(b, how='inner')", "inner join"),
        ("d = pd.to_datetime(col, errors='coerce')", "could not be parsed"),
        ("df['y'] = df['y'].astype(int)", "truncate or round"),
        ("w = df.resample('M').sum()", "time grain"),
        ("g = df.interpolate()", "not measured"),
    ],
)
def test_silent_decisions_in_code_are_reported(code: str, expected_fragment: str) -> None:
    """Each of these changes what the resulting number means."""
    notes = assumptions_from_code(code)
    assert any(expected_fragment in note for note in notes), notes


def test_clean_code_produces_no_caveats() -> None:
    assert assumptions_from_code("print(df['revenue'].sum())") == []
    assert assumptions_from_code("") == []


def test_each_assumption_is_reported_once() -> None:
    notes = assumptions_from_code("a.dropna()\nb.dropna()\nc.dropna()")
    assert len(notes) == 1


def test_loader_truncation_becomes_a_caveat() -> None:
    """A down-sampled frame makes every count a sample statistic, silently."""
    notes = assumptions_from_profile({"truncated": True, "rows": 200_000, "original_rows": 5_000_000})
    assert any("200,000" in note and "5,000,000" in note for note in notes)


def test_dropped_and_renamed_columns_are_reported() -> None:
    notes = assumptions_from_profile({"dropped_columns": ["notes"], "renamed_columns": {"a-b": "a_b"}})
    joined = " ".join(notes)
    assert "notes" in joined
    assert "a-b" in joined and "a_b" in joined


def test_a_clean_load_produces_no_caveats() -> None:
    assert assumptions_from_profile({"truncated": False, "rows": 10}) == []

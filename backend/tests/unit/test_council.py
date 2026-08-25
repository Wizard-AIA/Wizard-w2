"""Council specialists as thin adapters over the validation framework (Phase 6).

`VisualizerAgent` and `StatisticianAgent` no longer own their deterministic checks -- they call
`analysis.validation.semantic`/`statistical` and adapt the result to the council's response shape.
These tests pin that the adapted shape still behaves the way `TheCouncil.adjudicate` (and its one
regression test) expect, not the checks themselves, which are covered in test_analysis_validation.py.
"""

from __future__ import annotations

from src.core.agent.council import ArchitectAgent, StatisticianAgent, VisualizerAgent


async def test_visualizer_is_not_applicable_to_code_with_no_plot() -> None:
    review = await VisualizerAgent().review("plan", "print(df.sum())", "10")

    assert review["applicable"] is False
    assert review["feedback"] == []


async def test_visualizer_flags_a_chart_missing_a_title() -> None:
    review = await VisualizerAgent().review("plan", "plt.plot(df['x'])", "")

    assert review["applicable"] is True
    assert any("no title" in note for note in review["feedback"])


async def test_statistician_is_not_applicable_to_unrelated_work() -> None:
    review = await StatisticianAgent().review("count rows", "print(len(df))", "10")

    assert review["applicable"] is False
    assert review["feedback"] == []


async def test_statistician_asks_no_tip_when_a_p_value_is_already_reported() -> None:
    review = await StatisticianAgent().review("run a t-test", "stats.ttest_ind(a, b)", "Significant, p-value=0.01.")

    assert review["applicable"] is True
    assert review["feedback"] == []


async def test_architect_flags_a_pandas_antipattern() -> None:
    review = await ArchitectAgent().review("plan", "for index, row in df.iterrows(): pass", "")

    assert review["applicable"] is True
    assert review["feedback"]

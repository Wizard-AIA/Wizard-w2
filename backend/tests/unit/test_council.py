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


# --------------------------------------------------------------------------- #
# A reviewer is a model call like any other: bound by the data policy, and counted
# --------------------------------------------------------------------------- #
class _RecordingLLM:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def acomplete(self, prompt: str, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        return "Check the sample size."


CLAIM = ("run a t-test", "stats.ttest_ind(a, b)", "The groups differ significantly.")


async def test_a_reviewer_asks_nothing_when_the_policy_keeps_values_off_its_model(monkeypatch) -> None:
    from src.core.agent.council import ReviewGuard

    llm = _RecordingLLM()
    monkeypatch.setattr("src.core.agent.council.llm_provider", llm)

    review = await StatisticianAgent().review(*CLAIM, guard=ReviewGuard(redact=True))

    assert llm.calls == [], "execution output must not go to a model the policy does not trust with values"
    assert review["feedback"], "the deterministic finding stands without the model"


async def test_a_reviewer_carries_the_data_mode_and_session_and_reports_its_call(monkeypatch) -> None:
    from src.config import settings
    from src.core.agent.council import ReviewGuard

    llm = _RecordingLLM()
    monkeypatch.setattr("src.core.agent.council.llm_provider", llm)
    recorded: list[tuple] = []

    guard = ReviewGuard(data_mode="local-only", session_id="s-1", record=lambda *args: recorded.append(args))
    review = await StatisticianAgent().review(*CLAIM, guard=guard)

    assert len(llm.calls) == 1
    assert llm.calls[0]["data_mode"] == "local-only" and llm.calls[0]["session_id"] == "s-1"
    assert llm.calls[0]["max_tokens"] == settings.output_budget("review")
    assert [entry[0] for entry in recorded] == ["review"] and recorded[0][2] == len(llm.calls[0]["prompt"])
    assert "Check the sample size." in review["feedback"]


async def test_a_reviewer_without_a_guard_behaves_as_before(monkeypatch) -> None:
    llm = _RecordingLLM()
    monkeypatch.setattr("src.core.agent.council.llm_provider", llm)
    await StatisticianAgent().review(*CLAIM)
    assert len(llm.calls) == 1 and llm.calls[0]["data_mode"] is None

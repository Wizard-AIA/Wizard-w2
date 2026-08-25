"""Phase 11: the ten adversarial scenarios, run offline against the real orchestrator.

Every scenario here runs through `AnalysisOrchestrator.run()` with a `ScriptedLLM` -- the same
fixtures and stubs the rest of the suite uses -- so this file needs no model server, no Docker,
and no network, and gates in CI exactly like every other test. It grades a different thing than
`test_agent_loop.py`'s scenario-shaped tests, though: those pin one behaviour each as it was
added, phase by phase; this file assembles the ten PLAN.md scenarios into one suite, scored by the
shared agent-quality metrics in `scripts/benchmark_harness/grading.py` (provenance completeness,
appropriate uncertainty, correct refusal, tool-call budget, premature stopping, failure to stop,
cost), so a future change that quietly regresses one of them fails here even if no single
phase-specific test still covers it.

See `scripts/benchmark_harness/scenarios/__init__.py` for why four of the ten scenarios call a
`core.analysis.*` function directly rather than running through the loop -- an honest boundary,
not an oversight.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from src.config import settings
from src.core.agent.events import EventCollector, EventType
from src.core.agent.orchestrator import orchestrator
from src.core.session import Session, session_manager
from src.core.tools.catalog import CatalogEngine


SCENARIOS_DIR = Path(__file__).resolve().parents[3] / "scripts" / "benchmark_harness"
if str(SCENARIOS_DIR) not in sys.path:
    sys.path.insert(0, str(SCENARIOS_DIR))

from grading import (  # noqa: E402
    score_appropriate_uncertainty,
    score_correct_refusal,
    score_cost,
    score_failure_to_stop,
    score_premature_stopping,
    score_provenance_completeness,
    score_tool_call_budget,
)
from scenarios import (  # noqa: E402
    AMBIGUOUS_QUESTIONS,
    LOOP_SCENARIOS,
    UNAMBIGUOUS_QUESTION,
    LoopScenario,
    inappropriate_test_fixture,
    target_leakage_fixture,
)


def _scenario(scenario_id: str) -> LoopScenario:
    return next(s for s in LOOP_SCENARIOS if s.id == scenario_id)


@pytest.fixture
def run_scenario(monkeypatch, stub_llm):
    """Builds a session from a `LoopScenario`'s fixture and runs it through the real loop."""

    async def run(scenario: LoopScenario):
        session: Session = session_manager.create()
        try:
            if scenario.tier is not None:
                monkeypatch.setattr(settings, "AGENT_TIER", scenario.tier)
            if scenario.tables:
                for index, (name, frame) in enumerate(scenario.tables.items()):
                    session.add_dataset(name, frame, profile=CatalogEngine.analyze(frame), make_active=index == 0)
            elif scenario.dataframe is not None:
                session.add_dataset(
                    f"{scenario.id}.csv", scenario.dataframe, profile=CatalogEngine.analyze(scenario.dataframe)
                )

            stub = stub_llm(scenario.responses)
            collector = EventCollector()
            result = await orchestrator.run(
                session=session, instruction=scenario.instruction, mode="auto", emitter=collector
            )
            return result, collector, stub
        finally:
            session_manager.drop(session.id)

    return run


# --------------------------------------------------------------------------- #
# Loop scenarios
# --------------------------------------------------------------------------- #
async def test_obvious_analysis_wrong(run_scenario) -> None:
    result, _collector, stub = await run_scenario(_scenario("obvious_analysis_wrong"))

    assert result.status == "completed"
    assert result.verification == "MISMATCH: got 15 expected 99"
    refusal = score_correct_refusal(
        "obvious_analysis_wrong", result.answer, result.warnings, should_flag=True, must_mention=("not trustworthy",)
    )
    assert refusal.passed, refusal.reasons
    uncertainty = score_appropriate_uncertainty(
        "obvious_analysis_wrong",
        result.analysis,
        allowed_verdicts=("answerable_with_caveats", "insufficient_evidence", "cannot_answer"),
    )
    assert uncertainty.passed, uncertainty.reasons
    cost = score_cost("obvious_analysis_wrong", len(stub.prompts), max_calls=6)
    assert cost.passed, cost.reasons


async def test_dirty_join_key(run_scenario) -> None:
    result, _collector, _stub = await run_scenario(_scenario("dirty_join_key"))

    assert result.status == "completed"
    join_keys = result.analysis["understanding"]["join_keys"]
    assert any(entry["dirty"] for entry in join_keys), "the formatting mismatch should be caught structurally"
    assert any(f["category"] == "join" for f in result.analysis["critic_findings"])


async def test_missingness_flips_the_conclusion(run_scenario) -> None:
    result, _collector, _stub = await run_scenario(_scenario("missingness_flips_the_conclusion"))

    uncertainty = score_appropriate_uncertainty(
        "missingness_flips_the_conclusion", result.analysis, allowed_verdicts=("cannot_answer",)
    )
    assert uncertainty.passed, uncertainty.reasons
    assert len(result.analysis["confidence"]["reasons"]) >= 3


async def test_two_methods_disagree(run_scenario) -> None:
    result, collector, _stub = await run_scenario(_scenario("two_methods_disagree"))

    assert result.status == "completed"
    comparisons = collector.of_type(EventType.ROUTE_COMPARISON)
    assert len(comparisons) == 1
    assert comparisons[0].data["verdict"] == "disagree"
    assert comparisons[0].data["more_appropriate"] == "spearman_correlation"


async def test_simpsons_paradox(run_scenario) -> None:
    result, collector, _stub = await run_scenario(_scenario("simpsons_paradox"))

    assert any(event.data["category"] == "simpsons_paradox" for event in collector.of_type(EventType.CRITIC_FINDING))
    provenance = score_provenance_completeness("simpsons_paradox", result.analysis)
    assert provenance.passed, provenance.reasons


async def test_multi_step_investigation(run_scenario) -> None:
    result, _collector, stub = await run_scenario(_scenario("multi_step_investigation"))

    assert result.status == "completed"
    premature = score_premature_stopping("multi_step_investigation", result.iterations, min_iterations=2)
    assert premature.passed, premature.reasons
    budget = settings.budget_for("auto", None)
    tool_budget = score_tool_call_budget(
        "multi_step_investigation", result.iterations, max_iterations=budget.iterations
    )
    assert tool_budget.passed, tool_budget.reasons
    stop = score_failure_to_stop("multi_step_investigation", result.iterations, budget.iterations, result.status)
    assert stop.passed, stop.reasons
    cost = score_cost("multi_step_investigation", len(stub.prompts), max_calls=8)
    assert cost.passed, cost.reasons


# --------------------------------------------------------------------------- #
# Deterministic-layer scenarios -- see scenarios/__init__.py's module docstring.
# --------------------------------------------------------------------------- #
def test_target_leakage() -> None:
    from src.core.analysis.understanding import leakage_indicators

    indicators = leakage_indicators(target_leakage_fixture(), "label")

    assert indicators, "a feature identical to the target must be flagged as leakage"
    assert indicators[0]["feature"] == "leaky"


def test_statistically_inappropriate_request() -> None:
    from src.core.analysis import methods

    result = methods.run_method(
        "independent_t_test", inappropriate_test_fixture(), value_col="value", group_col="group"
    )

    assert result["status"] == "refused"
    assert result["alternative"] == "one_way_anova"


def test_insufficient_evidence() -> None:
    from src.core.analysis.confidence import ConfidenceContext, compute

    result = compute(ConfidenceContext())

    assert result.verdict == "insufficient_evidence"
    assert result.reasons


def test_ambiguous_question_is_left_unclassified_not_guessed() -> None:
    from src.core.analysis.objective import AnalyticalObjective

    for question in AMBIGUOUS_QUESTIONS:
        objective = AnalyticalObjective.infer(question)
        assert objective.analytical_type is None, f"{question!r} should not resolve to a guessed type"

    # The check has teeth: a clearly-typed question must still resolve to something.
    assert AnalyticalObjective.infer(UNAMBIGUOUS_QUESTION).analytical_type is not None

"""AnalyticalPlan and PlanRevision -- the mechanical step parser and the
append-only revision history ADR 0002 requires.
"""

from __future__ import annotations

from src.core.analysis.plan import AnalyticalPlan, PlanRevision, parse_intended_analyses


# --------------------------------------------------------------------------- #
# parse_intended_analyses
# --------------------------------------------------------------------------- #
def test_parses_numbered_steps() -> None:
    text = "1. Load the data\n2. Compute the mean\n3. Plot it"

    assert parse_intended_analyses(text) == ["Load the data", "Compute the mean", "Plot it"]


def test_parses_numbered_steps_with_parenthesis_style() -> None:
    text = "1) Load\n2) Compute"

    assert parse_intended_analyses(text) == ["Load", "Compute"]


def test_parses_bulleted_steps() -> None:
    text = "- Load the data\n* Compute the mean"

    assert parse_intended_analyses(text) == ["Load the data", "Compute the mean"]


def test_free_prose_yields_no_steps() -> None:
    """Never guesses at structure that isn't there -- see the module docstring."""
    text = "I will look at the data and then figure out what to do."

    assert parse_intended_analyses(text) == []


def test_ignores_non_list_lines_mixed_with_list_lines() -> None:
    text = "Here is my plan:\n1. First step\nSome commentary\n2. Second step"

    assert parse_intended_analyses(text) == ["First step", "Second step"]


def test_empty_and_none_input() -> None:
    assert parse_intended_analyses("") == []
    assert parse_intended_analyses(None) == []  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# PlanRevision
# --------------------------------------------------------------------------- #
def test_revision_round_trips_through_dict() -> None:
    revision = PlanRevision(index=2, text="1. Do the thing", why="because the data disagreed")

    restored = PlanRevision.from_dict(revision.to_dict())

    assert restored.index == revision.index
    assert restored.text == revision.text
    assert restored.why == revision.why


# --------------------------------------------------------------------------- #
# AnalyticalPlan
# --------------------------------------------------------------------------- #
def test_revise_appends_never_overwrites() -> None:
    plan = AnalyticalPlan()

    plan.revise("1. Original plan", why="initial")
    plan.revise("1. New plan", why="revised after evidence")

    assert len(plan.revisions) == 2
    assert plan.revisions[0].text == "1. Original plan"
    assert plan.revisions[1].text == "1. New plan"
    assert plan.revisions[0].index == 0
    assert plan.revisions[1].index == 1


def test_current_text_is_the_latest_revision() -> None:
    plan = AnalyticalPlan()
    assert plan.current_text == ""

    plan.revise("1. First")
    plan.revise("1. Second")

    assert plan.current_text == "1. Second"


def test_revise_re_derives_intended_analyses_from_the_latest_text() -> None:
    plan = AnalyticalPlan()

    plan.revise("1. Step A\n2. Step B")
    assert plan.intended_analyses == ["Step A", "Step B"]

    plan.revise("1. Step C")
    assert plan.intended_analyses == ["Step C"]


def test_revise_extracts_explicit_hypotheses_without_guessing() -> None:
    plan = AnalyticalPlan()
    plan.revise("Hypothesis: Revenue grew due to seasonality\n1. Compare monthly totals")

    assert plan.hypotheses == ["Revenue grew due to seasonality"]


def test_plan_round_trips_through_dict_including_all_revisions() -> None:
    plan = AnalyticalPlan(
        hypotheses=["revenue grew due to seasonality"],
        required_evidence=["monthly revenue by region"],
        assumptions=["no currency conversion issues"],
        dependencies=["dataset must include region"],
        expected_outputs=["a month-over-month chart"],
        validation_criteria=["independently recomputed total matches"],
        stop_criteria=["confidence is at least medium"],
        fallbacks=["fall back to a simple trend line"],
        open_uncertainties=["whether the spike is a data entry error"],
    )
    plan.revise("1. First", why="initial")
    plan.revise("1. Second", why="revised")

    restored = AnalyticalPlan.from_dict(plan.to_dict())

    assert restored.hypotheses == plan.hypotheses
    assert restored.required_evidence == plan.required_evidence
    assert restored.assumptions == plan.assumptions
    assert restored.dependencies == plan.dependencies
    assert restored.expected_outputs == plan.expected_outputs
    assert restored.validation_criteria == plan.validation_criteria
    assert restored.stop_criteria == plan.stop_criteria
    assert restored.fallbacks == plan.fallbacks
    assert restored.open_uncertainties == plan.open_uncertainties
    assert len(restored.revisions) == 2
    assert [revision.text for revision in restored.revisions] == ["1. First", "1. Second"]


def test_empty_plan_round_trips() -> None:
    restored = AnalyticalPlan.from_dict({})

    assert restored.revisions == []
    assert restored.hypotheses == []
    assert restored.current_text == ""

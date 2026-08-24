"""AnalyticalState and AnalyticalObjective -- construction, round-tripping, and the
findings/assumptions delegation to Investigation that keeps Investigation the single
source of truth during a live turn.
"""

from __future__ import annotations

from src.core.agent.actions import Investigation
from src.core.analysis.objective import AnalyticalObjective
from src.core.analysis.state import AnalyticalState


# --------------------------------------------------------------------------- #
# AnalyticalObjective
# --------------------------------------------------------------------------- #
def test_objective_round_trips_through_dict() -> None:
    objective = AnalyticalObjective(
        question="Did retention improve after the pricing change?",
        analytical_type="comparative",
        unit_of_analysis="customer",
        population="customers active in the last 12 months",
        time_dimension="monthly",
        likely_variables={"dependent": ["retained"], "independent": ["price_tier"]},
        constraints=["exclude trial accounts"],
        expected_output="a percentage point comparison",
        ambiguity=["'improved' could mean absolute or relative change"],
    )

    restored = AnalyticalObjective.from_dict(objective.to_dict())

    assert restored == objective


def test_objective_defaults_to_no_type_and_no_ambiguity() -> None:
    objective = AnalyticalObjective(question="How many rows are there?")

    assert objective.analytical_type is None
    assert objective.ambiguity == []
    assert objective.to_dict()["analytical_type"] is None


def test_unrecognised_analytical_type_is_dropped_not_guessed() -> None:
    """An unknown type is never coerced into a plausible-looking default -- see
    docs/analysis/state-model.md's serialisation rule.
    """
    restored = AnalyticalObjective.from_dict({"question": "x", "analytical_type": "clairvoyant"})

    assert restored.analytical_type is None
    assert any("clairvoyant" in note for note in restored.ambiguity)


def test_from_dict_tolerates_a_missing_question() -> None:
    restored = AnalyticalObjective.from_dict({})

    assert restored.question == ""
    assert restored.likely_variables == {}


# --------------------------------------------------------------------------- #
# AnalyticalState
# --------------------------------------------------------------------------- #
def test_state_defaults_are_all_empty_or_none() -> None:
    state = AnalyticalState()

    assert state.objective is None
    assert state.understanding is None
    assert state.hypotheses == []
    assert state.evidence_refs == []
    assert state.validations == []
    assert state.open_questions == []
    assert state.confidence is None
    assert state.findings == []
    assert state.assumptions == []


def test_findings_and_assumptions_read_live_from_investigation() -> None:
    investigation = Investigation()
    state = AnalyticalState(investigation=investigation)

    investigation.note_finding("revenue grew 12% quarter over quarter")
    investigation.note_assumption("nulls in the discount column were dropped before summing")

    assert state.findings == ["revenue grew 12% quarter over quarter"]
    assert state.assumptions == ["nulls in the discount column were dropped before summing"]


def test_findings_fall_back_to_snapshot_without_a_live_investigation() -> None:
    """Mirrors a state reloaded from storage (Phase 10), which has no Investigation attached."""
    state = AnalyticalState.from_dict({"findings": ["a"], "assumptions": ["b"]})

    assert state.investigation is None
    assert state.findings == ["a"]
    assert state.assumptions == ["b"]


def test_state_round_trips_through_dict_including_objective() -> None:
    objective = AnalyticalObjective(question="What drove the spike?", analytical_type="diagnostic")
    investigation = Investigation()
    investigation.note_finding("a single outlier region explains most of the spike")
    state = AnalyticalState(
        objective=objective,
        understanding={"grain": "one row per order"},
        hypotheses=[{"label": "regional promo", "status": "supported"}],
        evidence_refs=["node-1"],
        validations=[{"kind": "computational", "status": "verified"}],
        open_questions=["was the promo region-specific or timing coincidence?"],
        confidence={"overall": "medium"},
        investigation=investigation,
    )

    restored = AnalyticalState.from_dict(state.to_dict())

    assert restored.objective == objective
    assert restored.understanding == {"grain": "one row per order"}
    assert restored.hypotheses == [{"label": "regional promo", "status": "supported"}]
    assert restored.evidence_refs == ["node-1"]
    assert restored.validations == [{"kind": "computational", "status": "verified"}]
    assert restored.open_questions == ["was the promo region-specific or timing coincidence?"]
    assert restored.confidence == {"overall": "medium"}
    # The snapshot, not a live Investigation -- from_dict never reconstructs one.
    assert restored.findings == ["a single outlier region explains most of the spike"]


def test_to_dict_never_leaks_the_investigation_back_reference() -> None:
    state = AnalyticalState(investigation=Investigation())

    assert "investigation" not in state.to_dict()

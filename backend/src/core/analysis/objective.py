"""The structured form of a user's analytical question -- PLAN.md Layer 1.

Converts "what did they ask" into a checkable shape: analytical type, unit of analysis,
population, time dimension, likely variables, constraints, expected output. Ambiguity is a
first-class field, not an absence -- an unresolved reading is recorded, never silently guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, get_args


AnalyticalType = Literal[
    "descriptive",
    "diagnostic",
    "inferential",
    "predictive",
    "exploratory",
    "comparative",
    "causal_looking",
    "forecasting",
    "anomaly",
    "cohort",
    "segmentation",
    "longitudinal",
    "multi_table",
    "hypothesis_test",
    "model_based",
    "evidence_synthesis",
]

#: Used to validate a deserialised type without guessing at an unknown one.
ANALYTICAL_TYPES: frozenset[str] = frozenset(get_args(AnalyticalType))


@dataclass
class AnalyticalObjective:
    """The structured analytical intent behind one turn's question."""

    question: str
    analytical_type: str | None = None
    unit_of_analysis: str | None = None
    population: str | None = None
    time_dimension: str | None = None
    #: "dependent"/"independent" -> column names, when the question implies them.
    likely_variables: dict[str, list[str]] = field(default_factory=dict)
    constraints: list[str] = field(default_factory=list)
    expected_output: str | None = None
    #: Readings of the question that were not resolved -- reported, never picked silently.
    ambiguity: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "analytical_type": self.analytical_type,
            "unit_of_analysis": self.unit_of_analysis,
            "population": self.population,
            "time_dimension": self.time_dimension,
            "likely_variables": self.likely_variables,
            "constraints": self.constraints,
            "expected_output": self.expected_output,
            "ambiguity": self.ambiguity,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AnalyticalObjective:
        """Rebuilds an objective, never coercing an unrecognised type into a guess."""
        analytical_type = data.get("analytical_type")
        ambiguity = list(data.get("ambiguity") or [])
        if analytical_type is not None and analytical_type not in ANALYTICAL_TYPES:
            ambiguity.append(f"Unrecognised analytical_type '{analytical_type}' was dropped.")
            analytical_type = None
        return cls(
            question=str(data.get("question", "")),
            analytical_type=analytical_type,
            unit_of_analysis=data.get("unit_of_analysis"),
            population=data.get("population"),
            time_dimension=data.get("time_dimension"),
            likely_variables={key: list(value) for key, value in (data.get("likely_variables") or {}).items()},
            constraints=list(data.get("constraints") or []),
            expected_output=data.get("expected_output"),
            ambiguity=ambiguity,
        )

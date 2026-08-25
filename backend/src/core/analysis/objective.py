"""The structured form of a user's analytical question -- PLAN.md Layer 1.

Converts "what did they ask" into a checkable shape: analytical type, unit of analysis,
population, time dimension, likely variables, constraints, expected output. Ambiguity is a
first-class field, not an absence -- an unresolved reading is recorded, never silently guessed.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
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

#: `AnalyticalObjective.infer`'s keyword table -- more specific readings first, since an
#: instruction like "compare the correlation" should read as comparative before inferential.
_TYPE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("causal_looking", ("causes", "caused by", "because of", "leads to", "effect of", "impact of")),
    ("hypothesis_test", ("significant", "hypothesis", "statistically", "is there a difference")),
    ("comparative", ("compare", "versus", " vs ", "difference between", "which is higher", "which is better")),
    ("forecasting", ("forecast", "next quarter", "next year", "next month")),
    ("predictive", ("predict", "will ", "expected to", "likely to")),
    ("anomaly", ("anomaly", "outlier", "unusual", "unexpected")),
    ("cohort", ("cohort", "retention")),
    ("segmentation", ("segment", "cluster", "group by")),
    ("longitudinal", ("over time", "trend", "year over year", "month over month")),
    ("multi_table", ("join", "merge", "across tables")),
    ("model_based", ("model", "regression", "classify", "classification")),
    ("inferential", ("correlate", "correlation", "relationship", "associated", "association")),
    ("diagnostic", ("why did", "why is", "root cause")),
    ("exploratory", ("explore", "understand the", "overview", "summarize the data")),
    ("evidence_synthesis", ("synthesize", "across all sources")),
    ("descriptive", ("how many", "what is the", "total ", "average ", "count ", "sum of")),
)

#: Phrases that name the column right after them as the outcome, not a position or a type guess --
#: `resolve_variables` only ever fills `likely_variables['dependent']` from one of these.
_DEPENDENT_PHRASES: tuple[str, ...] = (
    "predict ",
    "predicting ",
    "target is ",
    "target: ",
    "outcome is ",
    "classify ",
)


def _mentions(text: str, name: str) -> bool:
    """Word-boundary match so a short column name (`id`, `n`) doesn't match inside another word --
    the same check `rag.retriever.mentions_column` uses, kept local to avoid coupling this leaf
    module to retrieval's heavier dependencies."""
    return re.search(rf"(?<![a-zA-Z0-9_]){re.escape(name.lower())}(?![a-zA-Z0-9_])", text) is not None


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
    def infer(cls, instruction: str) -> AnalyticalObjective:
        """A cheap, deterministic first reading of the question -- keyword-matched against
        this module's own `AnalyticalType` vocabulary, never a guess past what the wording
        actually says. `analytical_type` stays `None`, not a default, when nothing matches."""
        lowered = (instruction or "").lower()
        analytical_type = None
        for candidate, keywords in _TYPE_KEYWORDS:
            if any(keyword in lowered for keyword in keywords):
                analytical_type = candidate
                break
        return cls(question=instruction or "", analytical_type=analytical_type)

    def resolve_variables(self, columns: Sequence[str]) -> None:
        """Fills `likely_variables['dependent']` only from an explicit "predict/target/classify
        <column>" phrase that names a real column -- never from position or dtype, since a silent
        guess here is exactly the fabricated certainty this module exists to avoid (the same
        boundary `understanding.py` draws around leakage and temporal coverage)."""
        if self.likely_variables.get("dependent"):
            return
        lowered = self.question.lower()
        for phrase in _DEPENDENT_PHRASES:
            index = lowered.find(phrase)
            if index == -1:
                continue
            window = lowered[index : index + len(phrase) + 40]
            for column in columns:
                if _mentions(window, str(column)):
                    self.likely_variables["dependent"] = [str(column)]
                    return

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

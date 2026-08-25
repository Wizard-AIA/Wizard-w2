"""Explainable confidence -- PLAN.md Layer 7 / Rule 5 / ADR 0004.

Confidence is computed from named, independently-derived components, never a percentage a
language model invents. Each component maps to a fixed ordinal scale via a documented rubric
below; the overall verdict is a deterministic function of the components, never an average taken
in a model's head. A component with nothing to evaluate this turn is reported as `"unknown"`,
honestly, rather than interpolated -- confidence only gets richer as later phases populate more
of the signals it draws on, the same "report fewer components honestly" boundary the ADR draws.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


Level = Literal["low", "medium", "high", "unknown"]
Verdict = Literal["answerable", "answerable_with_caveats", "insufficient_evidence", "cannot_answer"]

#: Row counts below this cannot support most statistical claims; the tier below that is still a
#: modest sample. Documented here explicitly rather than left as an implicit assumption.
_SAMPLE_SIZE_LOW = 30
_SAMPLE_SIZE_MEDIUM = 200


@dataclass
class Component:
    """One named, independently graded input to the overall verdict."""

    name: str
    level: Level
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "level": self.level, "reason": self.reason}


@dataclass
class Confidence:
    """The deterministic verdict over a set of components, and why."""

    verdict: Verdict
    components: list[Component] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "components": [component.to_dict() for component in self.components],
            "reasons": self.reasons,
        }


@dataclass
class ConfidenceContext:
    """Signals confidence is derived from. Nothing here is fetched or guessed by this module --
    every field is something an earlier phase already established this turn."""

    completeness_score: float | None = None  # CatalogEngine's 0-100 `global_quality.completeness_score`
    row_count: int | None = None
    verification_status: str | None = None  # "verified" / "mismatch" / "inconclusive" / None
    route_comparison_verdict: str | None = None  # Phase 8's `RouteComparison.verdict`, when it ran
    has_sensitivity_finding: bool = False
    has_wrong_test_finding: bool = False
    unresolved_count: int = 0  # open questions + unresolved hypotheses + error-severity critic findings
    decision_inferred: bool = False  # any decision this turn was a fallback, not a reasoned choice


def _data_completeness(ctx: ConfidenceContext) -> Component:
    if ctx.completeness_score is None:
        return Component("data_completeness", "unknown", "No data-quality profile was computed this turn.")
    if ctx.completeness_score >= 95:
        return Component("data_completeness", "high", f"{ctx.completeness_score:.1f}% of cells are populated.")
    if ctx.completeness_score >= 80:
        return Component("data_completeness", "medium", f"{ctx.completeness_score:.1f}% of cells are populated.")
    return Component("data_completeness", "low", f"Only {ctx.completeness_score:.1f}% of cells are populated.")


def _sample_size(ctx: ConfidenceContext) -> Component:
    if ctx.row_count is None:
        return Component("sample_size", "unknown", "Row count was not available.")
    if ctx.row_count < _SAMPLE_SIZE_LOW:
        return Component("sample_size", "low", f"Only {ctx.row_count} rows -- too few to generalise confidently.")
    if ctx.row_count < _SAMPLE_SIZE_MEDIUM:
        return Component("sample_size", "medium", f"{ctx.row_count} rows -- a modest sample.")
    return Component("sample_size", "high", f"{ctx.row_count} rows.")


def _method_robustness(ctx: ConfidenceContext) -> Component:
    if ctx.has_wrong_test_finding:
        return Component("method_robustness", "low", "The critic flagged the chosen method as not fitting this data.")
    return Component("method_robustness", "unknown", "No method-applicability check ran against this turn's code.")


def _verification(ctx: ConfidenceContext) -> Component:
    if ctx.verification_status is None:
        return Component("verification", "unknown", "The result was not independently re-derived this turn.")
    if ctx.verification_status == "verified":
        return Component("verification", "high", "An independent recomputation matched the reported figure.")
    if ctx.verification_status == "mismatch":
        return Component("verification", "low", "An independent recomputation disagreed with the reported figure.")
    return Component("verification", "medium", f"Verification was inconclusive ({ctx.verification_status}).")


def _method_agreement(ctx: ConfidenceContext) -> Component:
    if ctx.route_comparison_verdict is None:
        return Component("method_agreement", "unknown", "No competing-method comparison ran this turn.")
    if ctx.route_comparison_verdict == "agree":
        return Component("method_agreement", "high", "Independent routes reached the same conclusion.")
    if ctx.route_comparison_verdict == "disagree":
        return Component("method_agreement", "low", "Independent routes disagreed on the conclusion.")
    return Component("method_agreement", "medium", "Competing routes did not produce a comparable conclusion.")


def _sensitivity(ctx: ConfidenceContext) -> Component:
    if ctx.has_sensitivity_finding:
        return Component("sensitivity", "low", "The result is sensitive to outliers in a column it aggregates.")
    return Component("sensitivity", "unknown", "No sensitivity check flagged this result.")


def _unresolved_assumptions(ctx: ConfidenceContext) -> Component:
    if ctx.unresolved_count == 0:
        return Component("unresolved_assumptions", "high", "No open questions or unresolved findings remain.")
    if ctx.unresolved_count <= 2:
        return Component("unresolved_assumptions", "medium", f"{ctx.unresolved_count} open item(s) remain unresolved.")
    return Component("unresolved_assumptions", "low", f"{ctx.unresolved_count} open items remain unresolved.")


def _model_uncertainty(ctx: ConfidenceContext) -> Component:
    if ctx.decision_inferred:
        return Component(
            "model_uncertainty", "medium", "At least one step this turn was a fallback choice, not a reasoned one."
        )
    return Component("model_uncertainty", "high", "Every step this turn was an explicit, reasoned choice.")


_COMPONENT_FUNCS = (
    _data_completeness,
    _sample_size,
    _method_robustness,
    _verification,
    _method_agreement,
    _sensitivity,
    _unresolved_assumptions,
    _model_uncertainty,
)


def _verdict(components: list[Component]) -> tuple[Verdict, list[str]]:
    """Deterministic rollup: known components decide the verdict; unknowns are silent, not counted
    against it. Three or more `low` components means the evidence itself is the problem, not just
    one weak link -- `cannot_answer` (a success state: naming the gap beats a confident guess)."""
    known = [component for component in components if component.level != "unknown"]
    low = [component for component in components if component.level == "low"]

    if not known:
        return "insufficient_evidence", ["No confidence components could be computed this turn."]
    if len(low) >= 3:
        return "cannot_answer", [component.reason for component in low]
    if low:
        return "answerable_with_caveats", [component.reason for component in low]
    if len(known) < 3:
        return "insufficient_evidence", ["Too few confidence components were available to be confident."]
    return "answerable", ["Every available confidence component was medium or high."]


def compute(ctx: ConfidenceContext) -> Confidence:
    """Computes every component, then rolls them up into one deterministic verdict."""
    components = [func(ctx) for func in _COMPONENT_FUNCS]
    verdict, reasons = _verdict(components)
    return Confidence(verdict=verdict, components=components, reasons=reasons)

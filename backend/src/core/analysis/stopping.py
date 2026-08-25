"""Analytical stop policy -- PLAN.md Layer 7.

Whether this turn's investigation should be reported as settled, deterministically -- no RL, no
learned policy. Named reasons, checked in order: the evidence threshold is already met,
validation is already complete without disqualifying it, another iteration's expected value is
too low to justify its cost, or the budget is exhausted with evidence still incomplete. Runs
alongside `analysis.confidence.compute` (see `orchestrator._compute_confidence`), since it needs
that rubric to have anything to weigh.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from src.core.analysis.confidence import Confidence


Reason = Literal["evidence_threshold_met", "validation_complete", "low_expected_value", "budget_exhausted", "continue"]


@dataclass
class StopDecision:
    """Whether continuing to investigate is still worth it, and why -- never a silent call."""

    should_stop: bool
    reason: Reason
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"should_stop": self.should_stop, "reason": self.reason, "detail": self.detail}


def decide(confidence: Confidence, *, iterations_used: int, iterations_budget: int, validated: bool) -> StopDecision:
    """`answerable` after verification needs nothing further. A budget already spent with
    unresolved evidence is named rather than silently accepted. Otherwise, whether pressing on is
    worth it turns on how much of the confidence rubric is still unknown versus already settled."""
    remaining = iterations_budget - iterations_used

    if confidence.verdict == "answerable" and validated:
        return StopDecision(
            True, "evidence_threshold_met", "Confidence is high and the result was independently verified."
        )
    if remaining <= 0:
        return StopDecision(
            True,
            "budget_exhausted",
            f"The iteration budget ({iterations_budget}) is spent; evidence remains {confidence.verdict}.",
        )
    if validated and confidence.verdict != "cannot_answer":
        return StopDecision(True, "validation_complete", "Validation ran and did not surface a disqualifying problem.")
    unknown = sum(1 for component in confidence.components if component.level == "unknown")
    if remaining <= 1 and unknown <= 2:
        return StopDecision(
            True,
            "low_expected_value",
            "Little of the confidence rubric remains unresolved; another iteration is unlikely to change the verdict.",
        )
    return StopDecision(False, "continue", "Evidence is still incomplete and iterations remain to address it.")

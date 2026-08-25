"""Research report: objective, hypotheses, methodology, and what the critic flagged.

For an analyst who wants to see how the question was approached and what could invalidate it, one
level short of the raw code and evidence graph -- see `technical.py` for that.
"""

from __future__ import annotations

from src.core.analysis.reports._shared import confidence_lines, warnings_lines
from src.core.analysis.reports.model import ReportModel


def _objective_lines(model: ReportModel) -> list[str]:
    objective = model.objective or {}
    analytical_type = objective.get("analytical_type") or "not classified"
    lines = ["## Objective", f"- Analytical type: {analytical_type}"]
    ambiguity = objective.get("ambiguity") or []
    lines.extend(f"- Ambiguity: {note}" for note in ambiguity)
    return lines


def _hypothesis_lines(model: ReportModel) -> list[str]:
    if not model.hypotheses:
        return []
    lines = ["## Hypotheses"]
    for hypothesis in model.hypotheses:
        support = len(hypothesis.get("evidence_for") or [])
        against = len(hypothesis.get("evidence_against") or [])
        lines.append(f"- [{hypothesis.get('status')}] {hypothesis.get('statement')} ({support} for, {against} against)")
    return lines


def _critic_lines(model: ReportModel) -> list[str]:
    if not model.critic_findings:
        return []
    lines = ["## Critic Findings"]
    for finding in model.critic_findings:
        lines.append(f"- **{finding.get('category')}** ({finding.get('severity')}): {finding.get('message')}")
    return lines


def _route_comparison_lines(model: ReportModel) -> list[str]:
    if not model.route_comparisons:
        return []
    lines = ["## Competing Methods"]
    for comparison in model.route_comparisons:
        lines.append(f"- {comparison.get('verdict')}: {comparison.get('agreement_detail')}")
        if comparison.get("more_appropriate"):
            lines.append(f"  - More appropriate: {comparison['more_appropriate']} -- {comparison.get('why')}")
    return lines


def render(model: ReportModel) -> str:
    lines = [
        "# Research Report",
        "",
        *_objective_lines(model),
        "",
        "## Question and Answer",
        model.instruction,
        "",
        model.answer or "_No answer was recorded for this turn._",
        "",
        *_hypothesis_lines(model),
        "",
        *_route_comparison_lines(model),
        "",
        *_critic_lines(model),
        "",
        "## Confidence",
        *confidence_lines(model),
        "",
        *warnings_lines(model),
    ]
    if len(model.plan_revisions) > 1:
        lines.append(f"_The plan was revised {len(model.plan_revisions) - 1} time(s) during this turn._")
    return "\n".join(lines).strip() + "\n"

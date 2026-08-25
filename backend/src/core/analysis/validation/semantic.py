"""Semantic validation: does a chart the code produced actually communicate anything.

The check itself is `council.VisualizerAgent`'s original logic, pulled out here so it has one
implementation instead of two; `council.py` now calls `check_chart_legibility` too, rather than
each keeping its own copy.
"""

from __future__ import annotations

from src.core.analysis.validation.base import Finding, ValidationContext


_PLOT_MARKERS = ("plt.", "sns.", "px.", "go.")


def check_chart_legibility(code: str) -> list[Finding]:
    if not any(marker in code for marker in _PLOT_MARKERS):
        return []

    findings: list[Finding] = []
    uses_matplotlib = "plt." in code or "sns." in code
    if uses_matplotlib:
        if "title" not in code:
            findings.append(Finding("semantic", "warning", "The chart has no title."))
        if "xlabel" not in code or "ylabel" not in code:
            findings.append(Finding("semantic", "warning", "The chart is missing one or both axis labels."))
    elif "title" not in code and "labels" not in code:
        findings.append(Finding("semantic", "warning", "The Plotly figure has no title or axis labels configured."))
    return findings


class SemanticValidator:
    name = "semantic"

    def applicable(self, ctx: ValidationContext) -> bool:
        return any(marker in ctx.code for marker in _PLOT_MARKERS)

    def validate(self, ctx: ValidationContext) -> list[Finding]:
        return check_chart_legibility(ctx.code)

"""Markdown fragments shared by all three renderers -- kept here so a wording change never has to
be made three times, without pulling shared *structure* decisions into one renderer's file.
"""

from __future__ import annotations

from src.core.analysis.reports.model import Claim, ReportModel


def confidence_lines(model: ReportModel) -> list[str]:
    confidence = model.confidence
    if not confidence:
        return ["_No confidence verdict was computed for this turn._"]
    verdict = str(confidence.get("verdict") or "unknown").replace("_", " ")
    lines = [f"**Verdict:** {verdict}"]
    lines.extend(f"- {reason}" for reason in confidence.get("reasons") or [])
    return lines


def warnings_lines(model: ReportModel) -> list[str]:
    if not model.warnings:
        return []
    return ["## Caveats", *[f"- {warning}" for warning in model.warnings], ""]


def claim_trace_line(claim: Claim) -> str:
    """One claim, cited back to the execution (and, transitively, the code and dataset) that
    produced it -- the ancestor chain `EvidenceGraph.trace` already computed, minus the claim's
    own node since its value is already shown."""
    ancestors = claim.provenance[:-1]
    if not ancestors:
        return f"- `{claim.value}` -- no execution recorded"
    return f"- `{claim.value}` ← {' ← '.join(reversed(ancestors))}"

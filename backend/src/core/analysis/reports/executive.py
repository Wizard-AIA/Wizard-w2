"""Executive report: the answer, whether it can be trusted, and why -- no code, no raw findings.

For someone deciding whether to act on the answer, not someone auditing how it was produced; see
`research.py`/`technical.py` for the same facts at increasing depth.
"""

from __future__ import annotations

from src.core.analysis.reports._shared import confidence_lines, warnings_lines
from src.core.analysis.reports.model import ReportModel


def render(model: ReportModel) -> str:
    lines = [
        "# Executive Summary",
        "",
        f"**Question:** {model.instruction}",
        "",
        model.answer or "_No answer was recorded for this turn._",
        "",
        *confidence_lines(model),
        "",
        *warnings_lines(model),
    ]
    traced = sum(1 for claim in model.claims if len(claim.provenance) > 1)
    if model.claims:
        lines.append(f"_{traced} of {len(model.claims)} figure(s) in this answer trace to a specific execution._")
    return "\n".join(lines).strip() + "\n"

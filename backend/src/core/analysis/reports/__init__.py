"""Evidence-backed reports -- PLAN.md Layer 9 / Phase 12.

Three renderers over one shared `ReportModel` (see `model.py`), built from an immutable
`AnalysisRun` rather than mutable live state -- a report always reflects what a turn's evidence
actually established at the moment it was captured (ADR 0005). The modes differ only in
presentation: `executive.py` is the answer plus a trust verdict, `research.py` adds objective,
hypotheses and critique, `technical.py` adds the full audit trail (dataset manifest, executed
code, every claim's provenance chain). None of the three calls an LLM -- every fact they render
is already deterministic, evidenced state; only phrasing would ever be a model's job, and there
is no established claim here that needs paraphrasing rather than reporting directly.
"""

from __future__ import annotations

from typing import Literal

from src.core.analysis.reports import executive, research, technical
from src.core.analysis.reports.model import Claim, ReportModel, build
from src.core.analysis.runs import AnalysisRun


ReportMode = Literal["executive", "research", "technical"]

_RENDERERS = {"executive": executive.render, "research": research.render, "technical": technical.render}


def render(run: AnalysisRun, mode: ReportMode = "executive") -> str:
    """Renders one run in one of the three modes, from the one shared model each renderer reads."""
    renderer = _RENDERERS.get(mode, executive.render)
    return renderer(build(run))


__all__ = ["Claim", "ReportModel", "ReportMode", "build", "render"]

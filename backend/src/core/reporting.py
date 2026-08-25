"""Executive summary generation.

Phase 12: reads immutable `AnalysisRun` snapshots (`db_mgr.get_recent_analysis_runs`), not
`working_memory` -- a report renders from what a turn's evidence actually established, with every
figure traceable to an execution, rather than from a free-text `result` string. The shared model
and the three renderers (executive/research/technical) live in `core.analysis.reports`; this
module is only the API-facing default and the interaction-count summary `/api/report` returns.
"""

from __future__ import annotations

from typing import Any

from src.core.analysis.reports import ReportMode, render
from src.core.analysis.runs import AnalysisRun
from src.core.database import db_mgr


class ReportingEngine:
    """Renders a session's recent analysis runs into a readable report."""

    @staticmethod
    def generate_executive_summary(
        timespan_seconds: int = 3600, session_id: str | None = None, mode: ReportMode = "executive"
    ) -> str:
        runs = db_mgr.get_recent_analysis_runs(session_id=session_id, timespan_seconds=timespan_seconds)
        if not runs:
            return (
                "## No analysis to summarise yet\n\n"
                "Run a few questions against your dataset and the report will collect the findings here."
            )
        sections = [render(AnalysisRun.from_dict(data), mode=mode) for data in runs]
        return "\n\n---\n\n".join(sections)

    @staticmethod
    def summary_payload(
        timespan_seconds: int = 3600, session_id: str | None = None, mode: ReportMode = "executive"
    ) -> dict[str, Any]:
        runs = db_mgr.get_recent_analysis_runs(session_id=session_id, timespan_seconds=timespan_seconds)
        report = ReportingEngine.generate_executive_summary(timespan_seconds, session_id, mode)
        return {"report": report, "interaction_count": len(runs)}


reporting_engine = ReportingEngine()

"""ReportingEngine (Phase 12): reads immutable `AnalysisRun` snapshots, not `working_memory` --
see `test_regressions.py::test_report_endpoint_does_not_raise_attribute_error` for the invariant
this replaced (an `AttributeError` from a working-memory attribute that no longer existed).

Message ids come from a real `db_mgr.append_chat_message` call, never a hardcoded literal: the
`analysis_runs` table's `message_id` is globally unique, and the suite shares one database file,
so a hardcoded id can collide with whatever another test already inserted -- the same reason
`test_export_api.py`'s `seed_message` does the same thing.
"""

from __future__ import annotations

import pandas as pd

from src.core.analysis.runs import capture, dataset_manifest_from_session
from src.core.database import db_mgr
from src.core.reporting import ReportingEngine
from src.core.session import Session


def _capture_run(session: Session, *, instruction: str, answer: str) -> int:
    message_id = db_mgr.append_chat_message(session.id, "assistant", answer, {"instruction": instruction})
    run = capture(
        session_id=session.id,
        message_id=message_id,
        instruction=instruction,
        answer=answer,
        dataset_manifest=dataset_manifest_from_session(session),
        steps=[],
        analysis={"evidence": {"nodes": [], "edges": []}, "confidence": {"verdict": "answerable", "reasons": []}},
        warnings=[],
    )
    db_mgr.save_analysis_run(session.id, message_id, run.to_dict())
    return message_id


def test_generate_executive_summary_is_the_placeholder_with_no_captured_runs(session: Session) -> None:
    report = ReportingEngine.generate_executive_summary(timespan_seconds=3600, session_id=session.id)

    assert "No analysis to summarise yet" in report


def test_generate_executive_summary_renders_a_captured_run(session: Session) -> None:
    session.add_dataset("a.csv", pd.DataFrame({"x": [1, 2, 3]}))
    _capture_run(session, instruction="how many rows", answer="3 rows.")

    report = ReportingEngine.generate_executive_summary(timespan_seconds=3600, session_id=session.id)

    assert "how many rows" in report
    assert "3 rows." in report


def test_generate_executive_summary_respects_the_requested_mode(session: Session) -> None:
    session.add_dataset("a.csv", pd.DataFrame({"x": [1, 2, 3]}))
    _capture_run(session, instruction="how many rows", answer="3 rows.")

    technical = ReportingEngine.generate_executive_summary(
        timespan_seconds=3600, session_id=session.id, mode="technical"
    )

    assert "a.csv" in technical


def test_generate_executive_summary_joins_multiple_runs_in_the_window(session: Session) -> None:
    session.add_dataset("a.csv", pd.DataFrame({"x": [1, 2, 3]}))
    _capture_run(session, instruction="first question", answer="first answer")
    _capture_run(session, instruction="second question", answer="second answer")

    report = ReportingEngine.generate_executive_summary(timespan_seconds=3600, session_id=session.id)

    assert "first question" in report
    assert "second question" in report


def test_generate_executive_summary_never_mixes_sessions(session: Session) -> None:
    _capture_run(session, instruction="mine", answer="mine answer")
    other_session_id = "a-reporting-test-different-session-id"
    other_run = capture(
        session_id=other_session_id,
        message_id=db_mgr.append_chat_message(other_session_id, "assistant", "not mine answer", {}),
        instruction="not mine",
        answer="not mine answer",
        dataset_manifest=[],
        steps=[],
        analysis={},
        warnings=[],
    )
    db_mgr.save_analysis_run(other_session_id, other_run.message_id, other_run.to_dict())

    report = ReportingEngine.generate_executive_summary(timespan_seconds=3600, session_id=session.id)

    assert "mine" in report
    assert "not mine" not in report


def test_summary_payload_reports_the_interaction_count(session: Session) -> None:
    session.add_dataset("a.csv", pd.DataFrame({"x": [1, 2, 3]}))
    _capture_run(session, instruction="q1", answer="a1")
    _capture_run(session, instruction="q2", answer="a2")

    payload = ReportingEngine.summary_payload(timespan_seconds=3600, session_id=session.id)

    assert payload["interaction_count"] == 2
